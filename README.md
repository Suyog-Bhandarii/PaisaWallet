# 👛 Paisa Wallet - Personal Finance & Merchant Hub

A modern, enterprise-grade digital wallet and merchant payment platform built with Python, Flask, SQLAlchemy, and a glassmorphic single-page frontend.

> ⚠️ **DISCLAIMER: SIMULATED PAYMENT SYSTEM**  
> Paisa Wallet is an educational and portfolio demonstration application. All currency amounts (NPR), peer-to-peer transfers, merchant checkouts, QR code settlements, and wallet top-ups are **entirely simulated**. There is **no connection to any real-world bank, credit card network, or financial gateway**.

---

## 🏛️ Financial Architecture & Core Principles

Paisa Wallet enforces financial correctness and bank-grade accounting principles across all operations:

1. **Zero-Float Financial Arithmetic (Integer Paisa)**:
   - Floats are strictly prohibited for monetary representation to eliminate IEEE 754 precision issues.
   - Balances and transaction amounts are stored in minor currency units (`balance_paisa`, `amount_paisa`, where `1 NPR = 100 Paisa`).
   - Inputs are parsed via Python `Decimal` with strict validation against non-finite values, exponential notation, or sub-paisa fractional precision.

2. **Balanced Double-Entry General Ledger**:
   - Every monetary transaction generates corresponding atomic `LedgerEntry` records (`DEBIT` and `CREDIT`) maintaining zero-sum systemic balance.
   - User account balances remain strictly reconciled with their respective ledger entry sums.
   - A built-in reconciliation utility (`reconciliation.py`) verifies ledger integrity and detects discrepancies.

3. **Concurrency Safety & Row-Level Locking**:
   - High-concurrency operations utilize row-level locking (`with_for_update`) with consistent user ID ordering to eliminate race conditions and avoid deadlocks.

4. **API Idempotency**:
   - Financial endpoints (`/api/send`, `/api/pay`, `/api/topup`) support the `Idempotency-Key` request header.
   - Payloads are hashed using SHA-256. Subsequent requests with the same key and payload return cached responses; mismatched payloads trigger `409 Conflict`.

5. **Financial Velocity Limits**:
   - **Single Transfer Limit**: NPR 50,000.00
   - **Daily Transfer Limit**: NPR 200,000.00
   - **Single Top-up Limit**: NPR 25,000.00
   - **Daily Top-up Limit**: NPR 100,000.00

---

## ✨ Features

- **Personal Wallet Dashboard**:
  - Live account balance badge and instant fund transfers.
  - Interactive ledger with real-time category filtering (`all`, `sent`, `paid`, `topup`).
  - Simulated wallet top-ups with quick-select presets (+500, +1000, +2500, +5000).
  - Public receipt verification (`GET /api/transactions/<reference>`).

- **Peer-to-Peer (P2P) Fund Transfers**:
  - Instant money transfer to any registered user by their mobile number.
  - Nepal mobile normalization supporting formats like `98XXXXXXXX`, `+977-98XXXXXXXX`, `97XXXXXXXX`.
  - Atomic database transactions with automated rollback on insufficient funds or invalid recipients.

- **Merchant Payments & Cryptographic QR Codes**:
  - User opt-in to become a registered merchant via `/merchant/register`.
  - Cryptographically secure, non-sequential QR codes (`qr_sec_...`).
  - Dynamic QR code generation (`/merchant/<id>/qr`) encoding direct payment links (`/pay/<qr_code_id>`).
  - Pre-filled merchant payment confirmation page with double-submit protection.
  - Merchant analytics dashboard tracking today's gross volume, today's payment count, and paginated customer settlements with strict tenant isolation.

- **Security & Reliability**:
  - **CSRF Protection (`Flask-WTF`)**: Enforced across all HTML POST forms and JavaScript `fetch()` calls via the `X-CSRFToken` header.
  - **Rate-Limiting (`Flask-Limiter`)**: Throttles login and financial write endpoints to reduce brute-force and automated transaction abuse.
  - **Account Lockout**: Automatically locks user accounts for 15 minutes after 5 consecutive failed login attempts.
  - **Strict 401 API Authentication**: Unauthenticated API requests receive HTTP 401 JSON instead of redirecting to the login page.
  - **Password Security**: Strong hashing via `Flask-Bcrypt` with password complexity enforcement.
  - **Security Headers**: Injects `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: strict-origin-when-cross-origin`.
  - **Hardened Cookies**: Session cookies configured with `HttpOnly`, `SameSite=Lax`, and conditional `Secure` flag.
  - **Readiness Probe**: `/api/ready` verifies database connectivity and schema health.

- **Modern Wallet Frontend**:
  - Dark-mode aesthetic with ambient glow effects, glassmorphic cards, micro-animations, and Google Fonts (`Outfit`, `Plus Jakarta Sans`).
  - Modal confirmation states with inline loaders (`.spinner`), idempotency key auto-generation, and structured API error handling.
  - Served from the Flask-integrated Jinja dashboard (`/dashboard`); the former duplicate standalone HTML frontend has been removed.

---

## 🛠️ Technology Stack

- **Backend**: Python 3.10+, Flask 3.x
- **Database / ORM**: SQLite (development/testing) & PostgreSQL (production) via Flask-SQLAlchemy 3.x
- **Database Migrations**: Flask-Migrate (Alembic) with batch migration mode enabled
- **Authentication**: Flask-Login & Flask-Bcrypt
- **Security**: Flask-WTF (CSRF) & Flask-Limiter (Rate Limiting)
- **QR Generation**: `qrcode` & `Pillow`
- **WSGI / Deployment**: Gunicorn & `wsgi.py` (Procfile included)
- **Frontend**: Vanilla HTML5, CSS3, JavaScript (Fetch API)

---

## ⚙️ Environment Variables

The application reads configuration from environment variables or a local `.env` file (via `python-dotenv`).

| Variable | Description | Default | Example |
| :--- | :--- | :--- | :--- |
| `SECRET_KEY` | Secret key for session encryption and CSRF tokens (**fails fast if insecure in production**) | `paisa-wallet-dev-secret-key...` | `a8f3b2c9...` (generate with `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `DATABASE_URL` | SQLAlchemy database connection URI | `sqlite:///instance/paisa_wallet.db` | `postgresql://user:pass@host:5432/paisa_db` |
| `FLASK_ENV` | Flask environment mode (`development`, `production`, `testing`) | `development` | `production` |
| `SESSION_COOKIE_SECURE` | Force HTTPS-only cookie transmission | `false` | `true` |
| `RATELIMIT_STORAGE_URI` | Storage backend for rate limiting | `memory://` | `redis://localhost:6379/0` |
| `PERMANENT_SESSION_LIFETIME` | Session duration in seconds | `86400` (24h) | `86400` |

> 💡 **Production PostgreSQL Note**: If deploying to cloud platforms (such as Render or Heroku) that provide legacy `postgres://` URLs, the application configuration automatically normalizes the prefix to `postgresql://` as required by SQLAlchemy.

---

## 🚀 Setup & Local Installation

### 1. Clone & Enter the Repository
```bash
git clone https://github.com/your-username/paisa-wallet.git
cd paisa-wallet
```

### 2. Set Up Virtual Environment
```bash
# On Linux/macOS:
python3 -m venv .venv
source .venv/bin/activate

# On Windows (PowerShell):
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
Copy the example environment file:
```bash
# On Linux/macOS:
cp .env.example .env

# On Windows (PowerShell):
Copy-Item .env.example .env
```

### 5. Run Database Migrations
Initialize and upgrade your database schema:
```bash
flask db upgrade
```

### 6. Seed Development Demo Accounts
To populate demo merchant accounts and sample balances for testing:
```bash
python seed_dev.py
```
*Creates demo accounts: Himalayan Java Coffee, Bhatbhateni Supermarket, and demo personal users.*

### 7. Run the Application
```bash
python app.py
```
The server will start at `http://127.0.0.1:5000`.

- **Landing Page**: `http://127.0.0.1:5000/`
- **Sign Up**: `http://127.0.0.1:5000/signup` (starts with NPR 0.00; top up via UI)
- **Login**: `http://127.0.0.1:5000/login`
- **Wallet Dashboard**: `http://127.0.0.1:5000/dashboard`
- **Readiness Probe**: `http://127.0.0.1:5000/api/ready`

---

## 🔍 Ledger Reconciliation & Auditing

To run an automated systemic reconciliation audit comparing all user balances against their ledger entries:

```bash
python reconciliation.py
```

Output:
```text
============================================================
              PAISA WALLET RECONCILIATION REPORT            
============================================================
Timestamp: 2026-09-22 18:00:00 UTC
Users Audited: 5
Discrepancies Found: 0
Total System Balance: NPR 12,450.00
Net Ledger Sum: NPR 12,450.00
System Zero-Sum Balanced: True
Status: RECONCILED (All balances match ledger records)
============================================================
```

---

## 🧪 Running Automated Tests & Linting

### Run the Test Suite
The repository includes regression and integration tests covering financial precision, ledger integrity, idempotency, rate limiting, security regressions, rollback/failure injection, concurrency, and merchant isolation:

```bash
python test_app.py

# PostgreSQL integration (requires TEST_POSTGRES_URL)
pytest -q test_postgres_integration.py
```

Expected output:
```text
Ran 34 tests in ~19s
OK
```

### Run Static Linting
```bash
ruff check .
```

---

## 🚢 Production Deployment

### Using Gunicorn (Linux / Container)
```bash
gunicorn wsgi:app --bind 0.0.0.0:5000 --workers 4
```

### PaaS Deployment (Render / Heroku / Railway / Fly.io)
A `Procfile` is pre-configured in the repository root:
```procfile
web: gunicorn wsgi:app
```
1. Connect your repository to the hosting platform.
2. Set environment variables in the platform dashboard (`SECRET_KEY`, `DATABASE_URL`, `SESSION_COOKIE_SECURE=true`).
3. Deploy! Run `flask db upgrade` in the release or build phase.

### Render Blueprint Deployment
This repository includes `render.yaml` for Render Blueprint deployment.

1. Push the repository to GitHub.
2. In Render, choose **New +** then **Blueprint** and select the GitHub repository.
3. Review the proposed `paisa-wallet` web service and `paisa-wallet-db` PostgreSQL database, then apply the blueprint.
4. Render generates `SECRET_KEY`, connects `DATABASE_URL`, enables secure cookies, runs migrations before Gunicorn, and checks `/api/health`.
5. After deployment, open the generated `onrender.com` URL and verify signup, login, dashboard loading, and the `/api/ready` endpoint.

The Blueprint currently uses `memory://` for Flask-Limiter so it can deploy without a second service. For production traffic across multiple instances, replace `RATELIMIT_STORAGE_URI` with a shared Redis URL in Render; do not use SQLite in production.

### Vercel Deployment
Vercel support is included through `api/index.py` and `vercel.json`.

1. Import the GitHub repository into Vercel.
2. Set the project environment variables `SECRET_KEY`, `DATABASE_URL`, `ENVIRONMENT=production`, `FLASK_ENV=production`, `SESSION_COOKIE_SECURE=true`, and `RATELIMIT_STORAGE_URI`.
3. Use an external PostgreSQL database. Vercel's filesystem is ephemeral, so do not use the default SQLite database.
4. Run database migrations separately with `flask --app wsgi:app db upgrade` against the production `DATABASE_URL` before using the application.

Vercel functions are serverless. Use a shared Redis URL for rate limiting when the application has more than one active function instance.

---

## 📡 REST API Reference

All protected API endpoints require an active session cookie and valid CSRF token (`X-CSRFToken` header) for POST requests. Unauthenticated requests return `401 Unauthorized`.

| Method | Endpoint | Description | Auth Required |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/csrf-token` | Returns `{ "status": "success", "csrf_token": "..." }` | No |
| `GET` | `/api/health` | Basic liveness probe | No |
| `GET` | `/api/ready` | Readiness probe (checks DB connectivity) | No |
| `GET` | `/api/wallet` | Returns user balance and profile | **Yes** |
| `GET` | `/api/transactions` | Query ledger (`?type=sent\|paid\|topup&page=1&per_page=10`) | **Yes** |
| `GET` | `/api/transactions/<ref>`| Public transaction receipt verification | No (authenticated users receive full receipt) |
| `POST` | `/api/send` | P2P transfer `{ "phone": "98...", "amount": 500 }` (supports `Idempotency-Key`) | **Yes** |
| `POST` | `/api/pay` | Pay merchant `{ "merchant_id": 1, "amount": 350 }` (supports `Idempotency-Key`) | **Yes** |
| `POST` | `/api/topup` | Add funds `{ "amount": 1000 }` (supports `Idempotency-Key`) | **Yes** |
| `GET` | `/api/merchant/dashboard`| Merchant revenue stats and paginated transactions | **Yes (Merchant)** |
| `POST` | `/merchant/register` | Opt in as a merchant `{ "name": "...", "emoji": "🏪" }` | **Yes** |
| `GET` | `/merchant/<id>/qr` | Returns PNG QR code for merchant payment URL | No |
| `GET` | `/pay/<qr_code_id>` | Payment confirmation page pre-filled with merchant name | **Yes** |

---

## 📄 License
MIT License. Built for demonstration and learning purposes.

## 🔒 Engineering Hardening (2026-09-23)

This release completes the five-phase financial/security hardening plan.

### Phase 1 — Financial correctness

- **Transactional idempotency:** financial API requests reserve the `(user, key, operation)` tuple before execution. The unique database constraint is the serialization point; completed responses are replayed exactly and conflicting payloads return `409`.
- **Daily limits inside the lock:** transfer, merchant-payment, and top-up daily counters are evaluated after the relevant wallet rows are locked.
- **Ledger invariants:** completed transfers/payments require balanced debit/credit entries; top-ups require a credit entry; reversals link to one original transaction. Ledger entries are append-only at the ORM layer.
- **Safe reversals:** reversals are admin-authorized, transactionally locked, linked to the original transaction, and idempotent. Supported reversals include P2P transfers, merchant payments, and simulated top-ups.
- **Concurrency coverage:** in-process SQLite concurrency tests are included, with PostgreSQL integration tests exercising the production locking path.

### Phase 2 — Security

- `.env` is excluded from the project; only `.env.example` is shipped. Rotate any previously exposed development secret before deployment.
- Production requires a strong `SECRET_KEY` of at least 32 characters; development can use an environment-provided secret or an ephemeral generated key.
- `/logout` is **POST-only** and protected by CSRF.
- Financial endpoints have dedicated Flask-Limiter controls.
- `/api/ready` returns a generic failure message and never exposes database exception text.
- `ProxyFix` is enabled only when `TRUSTED_PROXY_HOPS` is explicitly configured, preventing spoofed `X-Forwarded-For` headers from becoming the client identity by default.
- CSP uses per-response nonces for scripts, removes third-party JavaScript/font origins, and removes inline event handlers. Style attributes remain separately allowed for legacy presentation compatibility.

### Phase 3 — Architecture

- The duplicate `paisa_frontend.html` standalone frontend has been removed; `/dashboard` is the single supported UI.
- User and merchant name validation now matches the 100-character database columns.
- Explicit Python enums and database check constraints define account, role, merchant, transaction, ledger, and idempotency states.
- Financial operations live in `wallet_service.py`; HTTP handlers only validate input, manage idempotency, and serialize responses.
- An explicit `ADMIN` role and admin-only reversal endpoint provide authorization for administrative financial corrections.

### Phase 4 — Testing

- `test_app.py` includes regression tests for atomic rollback, ledger immutability/invariants, admin authorization, reversal idempotency, security headers, readiness error sanitization, idempotency replay, and SQLite concurrency.
- `test_postgres_integration.py` exercises concurrent transfers against PostgreSQL when `TEST_POSTGRES_URL` is configured.
- `docker-compose.test.yml` provides a repeatable PostgreSQL 16 test environment.
- CI runs the regular suite and a separate PostgreSQL integration job.
- Failure-injection tests verify that database commit failures do not leave partial wallet balances or ledger rows.

### Phase 5 — Documentation and deployment

- The signup behavior is documented accurately: new demo accounts receive a **simulated NPR 5,000 promotional welcome credit**, represented by an opening top-up ledger transaction. It is not real external funding.
- SQLite is explicitly development/test-only. PostgreSQL is the production database because SQLite does not provide row-level `SELECT ... FOR UPDATE` locking across application workers/processes.
- Production deployment should use PostgreSQL, a shared rate-limit backend such as Redis, HTTPS, a platform secret manager, trusted proxy configuration, migrations, monitoring, backups, and log/audit retention.

### Production security checklist

Before deploying:

- [ ] Set a unique random `SECRET_KEY` (32+ bytes) in the platform secret store.
- [ ] Do not deploy `.env` or commit secrets.
- [ ] Use PostgreSQL; do not use SQLite for multi-worker production traffic.
- [ ] Run `flask db upgrade` as part of the release process.
- [ ] Set `SESSION_COOKIE_SECURE=true` and terminate TLS correctly.
- [ ] Set `TRUSTED_PROXY_HOPS` only to the actual number of trusted reverse proxies.
- [ ] Use Redis or another shared persistent backend for Flask-Limiter.
- [ ] Restrict database/network access to the application and migration workers.
- [ ] Configure automated database backups and test restores.
- [ ] Monitor `/api/health` and `/api/ready` without exposing internal errors.
- [ ] Review admin accounts and keep the `ADMIN` role limited to authorized operators.
- [ ] Retain and protect audit logs; they are append-only at the application layer.
- [ ] Run the regular test suite and PostgreSQL integration tests before release.
- [ ] Review CSP/security headers after any frontend dependency change.

### PostgreSQL integration test

Start the local test database:

```bash
docker compose -f docker-compose.test.yml up -d postgres
```

Then run:

```bash
TEST_POSTGRES_URL=postgresql://paisa:paisa_test_password@127.0.0.1:54329/paisa_wallet_test \
pytest -q test_postgres_integration.py
```

The SQLite suite remains useful for fast local feedback, but it must not be interpreted as proof of multi-process row-locking behavior.
