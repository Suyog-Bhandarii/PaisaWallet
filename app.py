import io
import json
import os
from datetime import datetime, timedelta, timezone

import qrcode
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

from audit import log_audit_event
from config import Config
from idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    complete_idempotency_record,
    compute_request_hash,
    get_idempotency_key,
    reserve_idempotency_record,
)
from models import LedgerEntry, Merchant, Transaction, User, db
from money import format_npr, paisa_to_npr
from phone_utils import InvalidPhoneError, normalize_nepal_phone
from schemas import (
    ValidationError,
    api_error,
    validate_merchant_payload,
    validate_pay_payload,
    validate_send_payload,
    validate_signup_payload,
    validate_topup_payload,
)
from wallet_service import (
    AccountStatusError,
    AdminAuthorizationError,
    InsufficientBalanceError,
    InvalidAmountError,
    InvalidRecipientError,
    LimitExceededError,
    WalletError,
    pay_merchant,
    topup_wallet,
    transfer_funds,
)

# Extension instances
bcrypt = Bcrypt()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=Config.RATELIMIT_STORAGE_URI
)
migrate = Migrate()


def create_app(config_class=Config):
    """Application factory for Paisa Wallet."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    if app.config.get("TRUSTED_PROXY_HOPS", 0) > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=app.config["TRUSTED_PROXY_HOPS"],
            x_proto=app.config["TRUSTED_PROXY_HOPS"],
            x_host=app.config["TRUSTED_PROXY_HOPS"],
            x_port=app.config["TRUSTED_PROXY_HOPS"],
        )

    # Ensure instance directory exists for SQLite storage
    os.makedirs(app.instance_path, exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    # Apply database schema if running with in-memory DB or unmigrated dev SQLite
    with app.app_context():
        if app.config.get('TESTING') or 'sqlite:///:memory:' in app.config.get('SQLALCHEMY_DATABASE_URI', ''):
            db.create_all()

    # -------------------------------------------------------------------------
    # Security Headers Middleware
    # -------------------------------------------------------------------------
    @app.before_request
    def set_csp_nonce():
        import secrets
        from flask import g
        g.csp_nonce = secrets.token_urlsafe(24)

    @app.after_request
    def apply_security_headers(response):
        """Inject security headers with a per-response CSP nonce."""
        from flask import g
        nonce = getattr(g, 'csp_nonce', '')
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; "
            "style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    # -------------------------------------------------------------------------
    # Authentication & Error Handlers
    # -------------------------------------------------------------------------
    login_manager.login_view = 'login'
    login_manager.login_message = 'Please log in to access your wallet.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        """Return JSON 401 for API routes & async fetch requests; redirect for HTML pages."""
        if (
            request.path.startswith('/api/') or
            request.is_json or
            'application/json' in request.headers.get('Accept', '') or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        ):
            return api_error('unauthorized', 'Authentication required. Please log in to access this resource.', 401)
        flash(login_manager.login_message, login_manager.login_message_category)
        return redirect(url_for('login', next=request.path))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """Handle CSRF validation failures for both JSON API and web forms."""
        msg = e.description or 'CSRF token is missing or invalid.'
        if (
            request.path.startswith('/api/') or
            request.is_json or
            'application/json' in request.headers.get('Accept', '') or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        ):
            return api_error('csrf_error', msg, 400)
        flash('Session expired or invalid CSRF token. Please try again.', 'danger')
        return redirect(request.referrer or url_for('login'))

    @app.errorhandler(429)
    def ratelimit_handler(e):
        """Handle rate limit exceeded responses."""
        description = getattr(e, 'description', 'Too many requests. Please try again later.')
        if (
            request.path.startswith('/api/') or
            request.is_json or
            'application/json' in request.headers.get('Accept', '') or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        ):
            return api_error('rate_limit_exceeded', f'Rate limit exceeded: {description}', 429)
        flash(f'Too many attempts. {description}', 'danger')
        return render_template('login.html', title='Log In - Paisa Wallet'), 429

    def _prepare_idempotency(operation, data):
        idem_key = get_idempotency_key()
        if not idem_key:
            return None, None
        req_hash = compute_request_hash(data)
        try:
            record, is_new = reserve_idempotency_record(current_user.id, idem_key, operation, req_hash)
        except IdempotencyConflictError as exc:
            return None, api_error('idempotency_conflict', str(exc), 409)
        except IdempotencyInProgressError as exc:
            return None, api_error('idempotency_in_progress', str(exc), 409)
        if not is_new:
            if record.response_body and record.response_code is not None:
                return None, (jsonify(json.loads(record.response_body)), record.response_code)
            return None, api_error('idempotency_in_progress', 'A request with this idempotency key is already being processed.', 409)
        return record, None

    # -------------------------------------------------------------------------
    # Readiness and Health Check Probes
    # -------------------------------------------------------------------------
    @app.route('/api/health')
    def health_check():
        """JSON health check endpoint for automated status testing."""
        return jsonify({
            "status": "healthy",
            "app": "paisa-wallet",
            "message": "Hello World from Paisa Wallet!",
            "database": "connected"
        }), 200

    @app.route('/api/ready')
    def ready_check():
        """Readiness probe checking database connectivity."""
        try:
            db.session.execute(db.text('SELECT 1'))
            return jsonify({
                "status": "ready",
                "app": "paisa-wallet",
                "database": "ready"
            }), 200
        except Exception:
            return jsonify({
                "status": "not_ready",
                "app": "paisa-wallet",
                "message": "Database connectivity check failed."
            }), 503

    @app.route('/api/csrf-token', methods=['GET'])
    def api_csrf_token():
        """Retrieve a fresh CSRF token for JavaScript fetch requests."""
        token = generate_csrf()
        return jsonify({
            'status': 'success',
            'csrf_token': token
        }), 200

    # -------------------------------------------------------------------------
    # Merchant Management & QR Routes
    # -------------------------------------------------------------------------
    @app.route('/merchant/register', methods=['GET', 'POST'])
    @login_required
    @limiter.limit("10 per minute", methods=['POST'])
    def merchant_register():
        """
        Opt in to becoming a merchant.
        Creates a Merchant row and generates a cryptographically random qr_code_id.
        """
        if request.method == 'POST':
            payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            try:
                name, emoji = validate_merchant_payload(payload)
            except ValidationError as e:
                if request.is_json:
                    return api_error(e.code, e.message, e.status_code)
                flash(e.message, 'danger')
                return render_template('merchant_register.html', title='Register as Merchant')

            # Generate unique cryptographically secure QR code identifier
            while True:
                qr_code_id = Merchant.generate_qr_id()
                if not Merchant.query.filter_by(qr_code_id=qr_code_id).first():
                    break

            merchant = Merchant(
                name=name,
                emoji=emoji,
                qr_code_id=qr_code_id,
                owner_user_id=current_user.id,
                status='ACTIVE'
            )
            current_user.is_merchant = True

            db.session.add(merchant)
            db.session.commit()

            log_audit_event("merchant_registered", user_id=current_user.id, details={
                "merchant_id": merchant.id,
                "merchant_name": merchant.name,
                "qr_code_id": merchant.qr_code_id
            })

            if request.is_json:
                return jsonify({
                    'status': 'success',
                    'message': f"Merchant '{merchant.name}' registered successfully.",
                    'merchant': merchant.to_dict()
                }), 201

            flash(f"Congratulations! Your business '{merchant.name}' is registered.", 'success')
            return redirect(url_for('dashboard'))

        return render_template('merchant_register.html', title='Register as Merchant')

    @app.route('/merchant/<int:id>/qr', methods=['GET'])
    def merchant_qr(id):
        """
        Generate and return a PNG QR code encoding the payment URL for the merchant.
        Encodes a URL like: /pay/<merchant_qr_code_id>
        """
        merchant = db.session.get(Merchant, id)
        if not merchant:
            return api_error('not_found', 'Merchant not found.', 404)

        if merchant.status != 'ACTIVE':
            return api_error('merchant_inactive', f"Merchant '{merchant.name}' is inactive.", 400)

        # URL encoded in the QR code (pointing to the pay page)
        pay_url = url_for('pay_page', qr_code_id=merchant.qr_code_id, _external=True)

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(pay_url)
        qr.make(fit=True)

        img = qr.make_image(fill_color="#0b0f19", back_color="#ffffff")
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)

        return send_file(
            buffer,
            mimetype='image/png',
            download_name=f"qr_{merchant.qr_code_id}.png"
        )

    @app.route('/pay/<qr_code_id>', methods=['GET', 'POST'])
    @login_required
    @limiter.limit("30 per minute", methods=['POST'])
    def pay_page(qr_code_id):
        """
        Looks up the merchant and renders payment confirmation page.
        """
        merchant = Merchant.query.filter_by(qr_code_id=qr_code_id).first_or_404()
        if merchant.status != 'ACTIVE':
            flash(f"Merchant '{merchant.name}' is currently not accepting payments.", 'danger')
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            amount = request.form.get('amount')
            if not amount:
                flash('Please enter a payment amount.', 'danger')
                return render_template('pay_merchant.html', merchant=merchant, title=f"Pay {merchant.name}")

            try:
                tx = pay_merchant(current_user.id, merchant.id, amount)
                flash(
                    f"Payment of {format_npr(tx.amount_paisa)} to {merchant.name} was successful! Ref: {tx.reference}",
                    'success'
                )
                return redirect(url_for('dashboard'))
            except (InsufficientBalanceError, InvalidRecipientError, InvalidAmountError, LimitExceededError, AccountStatusError) as e:
                flash(str(e), 'danger')
            except Exception:
                flash('An unexpected error occurred while processing payment.', 'danger')

        return render_template('pay_merchant.html', merchant=merchant, title=f"Pay {merchant.name}")

    # -------------------------------------------------------------------------
    # JSON API Routes
    # -------------------------------------------------------------------------
    @app.route('/api/wallet', methods=['GET'])
    @login_required
    def api_wallet():
        """Retrieve the currently authenticated user's wallet profile and balance."""
        return jsonify({
            'status': 'success',
            'user': current_user.to_dict()
        }), 200

    @app.route('/api/merchant/dashboard', methods=['GET'])
    @login_required
    def api_merchant_dashboard():
        """
        For users where is_merchant is true, returns:
        - total received today
        - count of payments today
        - paginated list of incoming transactions to their merchant account
        Strictly isolated to merchants owned by current_user.
        """
        if not current_user.is_merchant:
            return api_error('forbidden', 'Forbidden. User is not a registered merchant.', 403)

        user_merchant_ids = [m.id for m in current_user.merchants]
        if not user_merchant_ids:
            merchant_query = Transaction.query.filter(db.false())
        else:
            merchant_query = Transaction.query.filter(
                Transaction.type == 'paid',
                Transaction.merchant_id.in_(user_merchant_ids)
            )

        # Metrics for today (UTC midnight to present)
        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        today_txs = merchant_query.filter(Transaction.timestamp >= start_of_today).all()
        total_paisa_today = sum(tx.amount_paisa for tx in today_txs)
        total_received_today = float(paisa_to_npr(total_paisa_today))
        count_payments_today = len(today_txs)

        # Pagination
        try:
            page = int(request.args.get('page', 1))
            per_page = int(request.args.get('per_page', 10))
            if page <= 0 or per_page <= 0:
                raise ValueError()
            per_page = min(per_page, 100)
        except (ValueError, TypeError):
            return api_error('invalid_parameter', 'page and per_page parameters must be positive integers.', 400)

        paginated = merchant_query.order_by(Transaction.timestamp.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return jsonify({
            'status': 'success',
            'is_merchant': True,
            'merchants': [m.to_dict() for m in current_user.merchants],
            'total_received_today': total_received_today,
            'total_received_today_paisa': total_paisa_today,
            'count_payments_today': count_payments_today,
            'transactions': [tx.to_dict() for tx in paginated.items],
            'pagination': {
                'page': paginated.page,
                'per_page': paginated.per_page,
                'total_items': paginated.total,
                'total_pages': paginated.pages,
                'has_next': paginated.has_next,
                'has_prev': paginated.has_prev
            }
        }), 200

    @app.route('/api/transactions', methods=['GET'])
    @login_required
    def api_transactions():
        """
        Retrieve paginated, filtered transaction history for current user.
        Query parameters:
          - type: 'sent', 'paid', 'topup', 'reversal' (optional)
          - page: page number (default 1)
          - per_page / limit: items per page (default 20, max 100)
        """
        type_filter = request.args.get('type', '').strip().lower()

        # Handle backward compatibility: limit maps to per_page
        per_page_raw = request.args.get('per_page') or request.args.get('limit', '20')
        page_raw = request.args.get('page', '1')

        try:
            page = int(page_raw)
            per_page = int(per_page_raw)
            if page <= 0 or per_page <= 0:
                raise ValueError()
            per_page = min(per_page, 100)
        except (ValueError, TypeError):
            return api_error('invalid_parameter', 'page and per_page must be positive integers.', 400)

        query = Transaction.query.filter(
            db.or_(
                Transaction.sender_id == current_user.id,
                Transaction.receiver_id == current_user.id
            )
        )

        if type_filter:
            valid_types = {'sent', 'paid', 'topup', 'reversal'}
            if type_filter not in valid_types:
                return api_error(
                    'invalid_parameter',
                    f"Invalid type filter '{type_filter}'. Must be one of: {', '.join(sorted(valid_types))}.",
                    400
                )
            query = query.filter_by(type=type_filter)

        paginated = query.order_by(Transaction.timestamp.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return jsonify({
            'status': 'success',
            'count': len(paginated.items),
            'transactions': [tx.to_dict() for tx in paginated.items],
            'pagination': {
                'page': paginated.page,
                'per_page': paginated.per_page,
                'total_items': paginated.total,
                'total_pages': paginated.pages,
                'has_next': paginated.has_next,
                'has_prev': paginated.has_prev
            }
        }), 200

    @app.route('/api/transactions/<reference>', methods=['GET'])
    def api_transaction_receipt(reference):
        """
        Lookup a transaction receipt by its public TXN reference.
        Authenticated participants receive full detail; others receive safe public receipt.
        """
        clean_ref = reference.strip()
        tx = Transaction.query.filter_by(reference=clean_ref).first()
        if not tx:
            return api_error('not_found', f"Transaction with reference '{clean_ref}' not found.", 404)

        # Check if caller is authenticated participant
        is_participant = bool(
            current_user.is_authenticated and (
                current_user.id in (tx.sender_id, tx.receiver_id)
                or (tx.merchant and tx.merchant.owner_user_id == current_user.id)
            )
        )

        if is_participant:
            # Full receipt detail
            return jsonify({
                'status': 'success',
                'access': 'full',
                'receipt': {
                    'reference': tx.reference,
                    'type': tx.type,
                    'amount_paisa': tx.amount_paisa,
                    'amount': tx.amount,
                    'currency': tx.currency,
                    'status': tx.status,
                    'sender_id': tx.sender_id,
                    'sender_name': tx.sender.name if tx.sender else None,
                    'sender_phone': tx.sender.phone_number if tx.sender else None,
                    'receiver_id': tx.receiver_id,
                    'receiver_name': tx.receiver.name if tx.receiver else None,
                    'receiver_phone': tx.receiver.phone_number if tx.receiver else None,
                    'merchant_id': tx.merchant_id,
                    'merchant_name': tx.merchant_name,
                    'timestamp': tx.timestamp.isoformat() if tx.timestamp else None,
                    'ledger_entries': [e.to_dict() for e in tx.ledger_entries]
                }
            }), 200

        # Public safe receipt
        sender_masked = f"{tx.sender.name[:1]}***" if tx.sender else None
        receiver_masked = f"{tx.receiver.name[:1]}***" if tx.receiver else None

        return jsonify({
            'status': 'success',
            'access': 'public',
            'receipt': {
                'reference': tx.reference,
                'type': tx.type,
                'amount': tx.amount,
                'currency': tx.currency,
                'status': tx.status,
                'sender': sender_masked,
                'receiver': receiver_masked,
                'merchant_name': tx.merchant_name,
                'timestamp': tx.timestamp.isoformat() if tx.timestamp else None,
                'verified': tx.status == 'completed'
            }
        }), 200

    @app.route('/api/send', methods=['POST'])
    @login_required
    @limiter.limit("30 per minute")
    def api_send():
        """
        Transfer funds to another user by phone number with idempotency support.
        Request body: { "phone": "9800000002", "amount": 500.0 }
        Header: Idempotency-Key (optional, recommended)
        """
        data = request.get_json(silent=True)
        try:
            clean_phone, amount_paisa = validate_send_payload(data)
        except ValidationError as e:
            return api_error(e.code, e.message, e.status_code)

        idem_record, idem_error = _prepare_idempotency('send', data)
        if idem_error:
            return idem_error

        try:
            tx = transfer_funds(current_user.id, clean_phone, amount_paisa, is_paisa=True, commit=idem_record is None)
            response_payload = {
                'status': 'success',
                'message': f'Successfully transferred {format_npr(tx.amount_paisa)} to {clean_phone}.',
                'transaction': tx.to_dict(),
                'balance': current_user.balance,
                'balance_paisa': current_user.balance_paisa
            }
            if idem_record is not None:
                complete_idempotency_record(idem_record, 200, response_payload)
                db.session.commit()
            return jsonify(response_payload), 200
        except InsufficientBalanceError as e:
            return api_error('insufficient_balance', str(e), 400)
        except InvalidRecipientError as e:
            return api_error('invalid_recipient', str(e), 400)
        except InvalidAmountError as e:
            return api_error('invalid_amount', str(e), 400)
        except LimitExceededError as e:
            return api_error('limit_exceeded', str(e), 400)
        except AccountStatusError as e:
            return api_error('account_inactive', str(e), 403)
        except WalletError as e:
            return api_error('wallet_error', str(e), 400)
        except Exception:
            return api_error('internal_error', 'Failed to complete transfer due to an internal error.', 500)

    @app.route('/api/pay', methods=['POST'])
    @login_required
    @limiter.limit("30 per minute")
    def api_pay():
        """
        Pay a merchant with idempotency support.
        Request body: { "merchant_id": 1, "amount": 350.0 }
        Header: Idempotency-Key (optional, recommended)
        """
        data = request.get_json(silent=True)
        try:
            merchant_id, amount_paisa = validate_pay_payload(data)
        except ValidationError as e:
            return api_error(e.code, e.message, e.status_code)

        idem_record, idem_error = _prepare_idempotency('pay', data)
        if idem_error:
            return idem_error

        try:
            tx = pay_merchant(current_user.id, merchant_id, amount_paisa, is_paisa=True, commit=idem_record is None)
            response_payload = {
                'status': 'success',
                'message': f'Successfully paid {format_npr(tx.amount_paisa)} to {tx.merchant_name}.',
                'transaction': tx.to_dict(),
                'balance': current_user.balance,
                'balance_paisa': current_user.balance_paisa
            }
            if idem_record is not None:
                complete_idempotency_record(idem_record, 200, response_payload)
                db.session.commit()
            return jsonify(response_payload), 200
        except InsufficientBalanceError as e:
            return api_error('insufficient_balance', str(e), 400)
        except InvalidRecipientError as e:
            return api_error('invalid_recipient', str(e), 400)
        except InvalidAmountError as e:
            return api_error('invalid_amount', str(e), 400)
        except LimitExceededError as e:
            return api_error('limit_exceeded', str(e), 400)
        except AccountStatusError as e:
            return api_error('account_inactive', str(e), 403)
        except WalletError as e:
            return api_error('wallet_error', str(e), 400)
        except Exception:
            return api_error('internal_error', 'Failed to process payment due to an internal error.', 500)

    @app.route('/api/topup', methods=['POST'])
    @login_required
    @limiter.limit("20 per minute")
    def api_topup():
        """
        Credit user wallet directly via simulated top-up with idempotency support.
        Request body: { "amount": 1000.0 }
        Header: Idempotency-Key (optional, recommended)
        """
        data = request.get_json(silent=True)
        try:
            amount_paisa = validate_topup_payload(data)
        except ValidationError as e:
            return api_error(e.code, e.message, e.status_code)

        idem_record, idem_error = _prepare_idempotency('topup', data)
        if idem_error:
            return idem_error

        try:
            tx = topup_wallet(current_user.id, amount_paisa, is_paisa=True, commit=idem_record is None)
            response_payload = {
                'status': 'success',
                'message': f'Successfully topped up {format_npr(tx.amount_paisa)}.',
                'transaction': tx.to_dict(),
                'balance': current_user.balance,
                'balance_paisa': current_user.balance_paisa
            }
            if idem_record is not None:
                complete_idempotency_record(idem_record, 200, response_payload)
                db.session.commit()
            return jsonify(response_payload), 200
        except InvalidAmountError as e:
            return api_error('invalid_amount', str(e), 400)
        except LimitExceededError as e:
            return api_error('limit_exceeded', str(e), 400)
        except AccountStatusError as e:
            return api_error('account_inactive', str(e), 403)
        except WalletError as e:
            return api_error('wallet_error', str(e), 400)
        except Exception:
            return api_error('internal_error', 'Failed to process top-up due to an internal error.', 500)

    # -------------------------------------------------------------------------
    # Web Authentication & UI Routes
    # -------------------------------------------------------------------------
    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        """User registration with phone normalization and starting balance."""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            try:
                name, clean_phone, password = validate_signup_payload(payload)
            except ValidationError as e:
                if request.is_json:
                    return api_error(e.code, e.message, e.status_code)
                flash(e.message, 'danger')
                return render_template('signup.html', title='Sign Up - Paisa Wallet')

            # Verify password confirmation if provided via form
            confirm = payload.get('confirm_password')
            if confirm and password != confirm:
                err = 'Passwords do not match. Please verify and re-enter.'
                if request.is_json:
                    return api_error('password_mismatch', err, 400)
                flash(err, 'danger')
                return render_template('signup.html', title='Sign Up - Paisa Wallet')

            # Verify password strength: at least 1 digit and 1 letter
            if not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
                err = 'Password must contain at least one letter and one number.'
                if request.is_json:
                    return api_error('weak_password', err, 400)
                flash(err, 'danger')
                return render_template('signup.html', title='Sign Up - Paisa Wallet')

            # Check for existing account
            existing = User.query.filter_by(phone_number=clean_phone).first()
            if existing:
                err = 'An account with this mobile number already exists. Please log in.'
                if request.is_json:
                    return api_error('user_exists', err, 400)
                flash(err, 'danger')
                return render_template('signup.html', title='Sign Up - Paisa Wallet')

            # The signup grant is a simulated promotional credit, not real external funds.
            # User creation and its opening ledger entry commit atomically.
            new_user = User(
                name=name,
                phone_number=clean_phone,
                balance_paisa=500_000,
                currency='NPR',
                status='ACTIVE',
                is_merchant=False
            )
            new_user.set_password(bcrypt, password)
            db.session.add(new_user)
            db.session.flush()

            initial_tx = Transaction(
                reference=Transaction.generate_reference(),
                sender_id=None,
                receiver_id=new_user.id,
                merchant_id=None,
                type='topup',
                amount_paisa=500_000,
                status='completed'
            )
            db.session.add(initial_tx)
            db.session.flush()

            ledger_entry = LedgerEntry(
                transaction_id=initial_tx.id,
                user_id=new_user.id,
                amount_paisa=500_000,
                entry_type='credit',
                balance_after_paisa=new_user.balance_paisa
            )
            db.session.add(ledger_entry)
            db.session.commit()

            log_audit_event("user_signup", user_id=new_user.id, details={
                "phone": clean_phone,
                "initial_balance_paisa": 500_000
            })

            login_user(new_user)
            if request.is_json:
                return jsonify({
                    'status': 'success',
                    'message': f'Namaste {new_user.name}! Your account has been created.',
                    'user': new_user.to_dict()
                }), 201

            flash(f'Namaste {new_user.name}! Your account has been created with NPR 5,000 welcome balance.', 'success')
            return redirect(url_for('dashboard'))

        return render_template('signup.html', title='Sign Up - Paisa Wallet')

    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit("5 per minute", methods=["POST"])
    def login():
        """User login route with credential verification, lockout protection, and status checks."""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            phone_raw = payload.get('phone_number') or payload.get('phone') or ''
            password = payload.get('password') or ''

            if not phone_raw or not password:
                err = 'Please provide both mobile number and password.'
                if request.is_json:
                    return api_error('missing_field', err, 400)
                flash(err, 'danger')
                return render_template('login.html', title='Log In - Paisa Wallet')

            try:
                clean_phone = normalize_nepal_phone(phone_raw)
            except InvalidPhoneError:
                clean_phone = phone_raw.strip()

            user = User.query.filter_by(phone_number=clean_phone).first()

            if user:
                # Check if user account is locked
                if user.locked_until:
                    now = datetime.now(timezone.utc)
                    locked_until_utc = user.locked_until
                    if locked_until_utc.tzinfo is None:
                        locked_until_utc = locked_until_utc.replace(tzinfo=timezone.utc)
                    if now < locked_until_utc:
                        minutes_left = max(1, int((locked_until_utc - now).total_seconds() / 60))
                        err = f"Account temporarily locked due to too many failed attempts. Try again in {minutes_left} minutes."
                        if request.is_json:
                            return api_error('account_locked', err, 403)
                        flash(err, 'danger')
                        return render_template('login.html', title='Log In - Paisa Wallet')
                    else:
                        # Lockout expired, reset attempts
                        user.failed_login_attempts = 0
                        user.locked_until = None
                        db.session.commit()

                # Check account status
                if user.status != 'ACTIVE':
                    err = f"Account status is '{user.status}'. Please contact customer support."
                    if request.is_json:
                        return api_error('account_inactive', err, 403)
                    flash(err, 'danger')
                    return render_template('login.html', title='Log In - Paisa Wallet')

                # Verify credentials
                if user.check_password(bcrypt, password):
                    user.failed_login_attempts = 0
                    user.locked_until = None
                    db.session.commit()

                    login_user(user)
                    log_audit_event("user_login_success", user_id=user.id)

                    if request.is_json:
                        return jsonify({
                            'status': 'success',
                            'message': f'Welcome back, {user.name}!',
                            'user': user.to_dict()
                        }), 200

                    flash(f'Welcome back, {user.name}!', 'success')
                    next_page = request.args.get('next')
                    if next_page and next_page.startswith('/') and not next_page.startswith('//'):
                        return redirect(next_page)
                    return redirect(url_for('dashboard'))
                else:
                    user.failed_login_attempts += 1
                    if user.failed_login_attempts >= 5:
                        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                        log_audit_event("account_locked", user_id=user.id, details={"failed_attempts": user.failed_login_attempts})
                    db.session.commit()
                    log_audit_event("user_login_failed", user_id=user.id)

            err = 'Invalid mobile number or password. Please try again.'
            if request.is_json:
                return api_error('invalid_credentials', err, 401)
            flash(err, 'danger')
            return render_template('login.html', title='Log In - Paisa Wallet')

        return render_template('login.html', title='Log In - Paisa Wallet')

    @app.route('/logout', methods=['POST'])
    @login_required
    def logout():
        """User logout route terminating session."""
        user_id = current_user.id
        logout_user()
        log_audit_event("user_logout", user_id=user_id)
        flash('You have been logged out safely.', 'info')
        return redirect(url_for('login'))

    @app.route('/api/admin/transactions/<int:transaction_id>/reverse', methods=['POST'])
    @login_required
    @limiter.limit("10 per minute")
    def admin_reverse_transaction(transaction_id):
        """Administrator-only, auditable transaction reversal endpoint."""
        from wallet_service import reverse_transaction
        data = request.get_json(silent=True) or {}
        reason = str(data.get('reason') or 'Administrative correction').strip()[:500]
        try:
            tx = reverse_transaction(transaction_id, actor_user_id=current_user.id, reason=reason)
            return jsonify({'status': 'success', 'transaction': tx.to_dict()}), 200
        except AdminAuthorizationError as e:
            return api_error(e.code, str(e), 403)
        except WalletError as e:
            return api_error(e.code, str(e), 400)
        except Exception:
            return api_error('internal_error', 'Failed to reverse transaction.', 500)

    # -------------------------------------------------------------------------
    # Protected Wallet UI Routes
    # -------------------------------------------------------------------------
    @app.route('/dashboard')
    @app.route('/wallet')
    @login_required
    def dashboard():
        """Protected wallet dashboard showing user balance and transaction history."""
        sample_merchants = Merchant.query.filter_by(status='ACTIVE').limit(4).all()
        return render_template('dashboard.html', title='My Wallet - Paisa Wallet', sample_merchants=sample_merchants)

    # -------------------------------------------------------------------------
    # Public Landing Page
    # -------------------------------------------------------------------------
    @app.route('/')
    def hello_world():
        """Landing page confirming app status and linking to wallet features."""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        stack_status = {
            "app_name": "Paisa Wallet",
            "framework": "Flask 3.x",
            "database": "SQLAlchemy (PostgreSQL / SQLite)",
            "auth": "Flask-Login (Session-based)",
            "security": "Flask-Bcrypt (Password Hashing)",
            "status": "Running smoothly"
        }
        return render_template('index.html', title='Paisa Wallet - Welcome', stack_status=stack_status)

    return app


app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
