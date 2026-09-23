"""Atomic wallet/accounting services.

Financial rules live here, not in Flask route handlers. PostgreSQL row locks provide
cross-process serialization; a small in-process lock supplements SQLite development
and test runs, which do not implement SELECT ... FOR UPDATE.
"""
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from threading import RLock

from sqlalchemy import func

from audit import log_audit_event
from enums import AccountStatus, LedgerEntryType, MerchantStatus, TransactionStatus, TransactionType, UserRole
from models import LedgerEntry, Merchant, Transaction, User, db
from money import DEFAULT_CURRENCY, format_npr, npr_to_paisa
from money import InvalidAmountError as MoneyInvalidAmountError
from phone_utils import InvalidPhoneError, normalize_nepal_phone

MAX_TRANSFER_AMOUNT_PAISA = 5_000_000
MAX_DAILY_TRANSFER_PAISA = 20_000_000
MAX_TOPUP_AMOUNT_PAISA = 2_500_000
MAX_DAILY_TOPUP_PAISA = 10_000_000

_user_locks: dict[tuple[int, ...], RLock] = {}
_user_locks_guard = RLock()


class WalletError(Exception):
    def __init__(self, message: str, code: str = "wallet_error"):
        super().__init__(message)
        self.message = message
        self.code = code


class InsufficientBalanceError(WalletError):
    def __init__(self, message: str):
        super().__init__(message, "insufficient_balance")


class InvalidRecipientError(WalletError):
    def __init__(self, message: str):
        super().__init__(message, "invalid_recipient")


class InvalidAmountError(WalletError):
    def __init__(self, message: str):
        super().__init__(message, "invalid_amount")


class LimitExceededError(WalletError):
    def __init__(self, message: str):
        super().__init__(message, "limit_exceeded")


class AccountStatusError(WalletError):
    def __init__(self, message: str):
        super().__init__(message, "account_inactive")


class AdminAuthorizationError(WalletError):
    def __init__(self, message: str = "Administrator authorization is required."):
        super().__init__(message, "admin_required")


def _operation_lock(user_ids: list[int]):
    key = tuple(sorted(set(user_ids)))
    if not key:
        return nullcontext()
    with _user_locks_guard:
        lock = _user_locks.setdefault(key, RLock())
    return lock


def _daily_sum(user_id: int, transaction_type: tuple[str, ...]) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = db.session.query(func.coalesce(func.sum(Transaction.amount_paisa), 0)).filter(
        Transaction.sender_id == user_id,
        Transaction.type.in_(transaction_type),
        Transaction.status == TransactionStatus.COMPLETED.value,
        Transaction.timestamp >= start,
    ).scalar()
    return int(result or 0)


def _get_daily_sent_paisa(user_id: int) -> int:
    return _daily_sum(user_id, (TransactionType.SENT.value, TransactionType.PAID.value))


def _get_daily_topup_paisa(user_id: int) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = db.session.query(func.coalesce(func.sum(Transaction.amount_paisa), 0)).filter(
        Transaction.receiver_id == user_id,
        Transaction.type == TransactionType.TOPUP.value,
        Transaction.status == TransactionStatus.COMPLETED.value,
        Transaction.timestamp >= start,
    ).scalar()
    return int(result or 0)


def _parse_amount(amount, *, is_paisa: bool, max_amount: int, label: str) -> int:
    if is_paisa:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise InvalidAmountError("Paisa amount must be a positive integer.")
        amount_paisa = amount
    else:
        try:
            amount_paisa = npr_to_paisa(amount)
        except MoneyInvalidAmountError as exc:
            raise InvalidAmountError(str(exc)) from exc
    if amount_paisa <= 0:
        raise InvalidAmountError(f"{label} amount must be strictly greater than zero.")
    if amount_paisa > max_amount:
        raise LimitExceededError(
            f"{label} amount exceeds maximum limit of {format_npr(max_amount)}."
        )
    return amount_paisa


def _validate_ledger_for_transaction(transaction: Transaction) -> None:
    """Validate double-entry shape before commit."""
    entries = list(transaction.ledger_entries)
    debits = sum(e.amount_paisa for e in entries if e.entry_type == LedgerEntryType.DEBIT.value)
    credits = sum(e.amount_paisa for e in entries if e.entry_type == LedgerEntryType.CREDIT.value)
    if transaction.type in (TransactionType.SENT.value, TransactionType.PAID.value):
        if len(entries) != 2 or debits != transaction.amount_paisa or credits != transaction.amount_paisa:
            raise WalletError(f"Ledger invariant failed for transaction {transaction.reference}.")
    elif transaction.type == TransactionType.TOPUP.value:
        if len(entries) != 1 or credits != transaction.amount_paisa:
            raise WalletError(f"Top-up ledger invariant failed for transaction {transaction.reference}.")
    elif transaction.type == TransactionType.REVERSAL.value:
        if transaction.reversed_transaction_id is None:
            raise WalletError(f"Reversal transaction {transaction.reference} is missing its original transaction link.")
        if debits != transaction.amount_paisa:
            raise WalletError(f"Reversal ledger invariant failed for transaction {transaction.reference}.")
        if credits not in (0, transaction.amount_paisa):
            raise WalletError(f"Reversal credit invariant failed for transaction {transaction.reference}.")


def _finish_transaction(transaction: Transaction, *, commit: bool) -> Transaction:
    db.session.flush()
    _validate_ledger_for_transaction(transaction)
    if commit:
        db.session.commit()
    return transaction


def transfer_funds(sender_id: int, receiver_phone: str, amount: str | float | Decimal, *, is_paisa: bool = False, commit: bool = True) -> Transaction:
    amount_paisa = _parse_amount(amount, is_paisa=is_paisa, max_amount=MAX_TRANSFER_AMOUNT_PAISA, label="Transfer")
    try:
        clean_phone = normalize_nepal_phone(receiver_phone)
    except InvalidPhoneError as exc:
        raise InvalidRecipientError(str(exc)) from exc

    sender_probe = db.session.query(User.id).filter_by(id=sender_id).first()
    receiver_probe = db.session.query(User.id).filter_by(phone_number=clean_phone).first()
    if not sender_probe:
        raise InvalidRecipientError(f"Sender with ID {sender_id} does not exist.")
    if not receiver_probe:
        raise InvalidRecipientError(f"Recipient with mobile number '{clean_phone}' was not found.")
    if sender_probe.id == receiver_probe.id:
        raise InvalidRecipientError("Cannot transfer funds to your own wallet.")

    try:
        with _operation_lock([sender_probe.id, receiver_probe.id]):
            first_id, second_id = sorted([sender_probe.id, receiver_probe.id])
            first = db.session.query(User).filter_by(id=first_id).with_for_update().first()
            second = db.session.query(User).filter_by(id=second_id).with_for_update().first()
            sender = first if first.id == sender_probe.id else second
            receiver = first if first.id == receiver_probe.id else second

            if not sender or not receiver:
                raise InvalidRecipientError("Sender or recipient account could not be found.")
            if not sender.is_active_account():
                raise AccountStatusError(f"Sender account status is '{sender.status}'. Cannot perform transfers.")
            if receiver.phone_number != clean_phone:
                raise InvalidRecipientError(f"Recipient with mobile number '{clean_phone}' was not found.")
            if not receiver.is_active_account():
                raise AccountStatusError(f"Recipient account status is '{receiver.status}'. Cannot receive funds.")

            # Daily limit is intentionally evaluated after locks are acquired.
            daily_sent = _get_daily_sent_paisa(sender.id)
            if daily_sent + amount_paisa > MAX_DAILY_TRANSFER_PAISA:
                raise LimitExceededError(
                    f"Daily transfer limit of {format_npr(MAX_DAILY_TRANSFER_PAISA)} exceeded. Today sent: {format_npr(daily_sent)}."
                )
            if sender.balance_paisa < amount_paisa:
                raise InsufficientBalanceError(
                    f"Insufficient balance. Available: {format_npr(sender.balance_paisa)}, Required: {format_npr(amount_paisa)}"
                )

            sender.balance_paisa -= amount_paisa
            receiver.balance_paisa += amount_paisa
            transaction = Transaction(
                sender_id=sender.id, receiver_id=receiver.id, type=TransactionType.SENT.value,
                amount_paisa=amount_paisa, currency=DEFAULT_CURRENCY, status=TransactionStatus.COMPLETED.value,
            )
            db.session.add(transaction)
            db.session.flush()
            db.session.add_all([
                LedgerEntry(transaction_id=transaction.id, user_id=sender.id, amount_paisa=amount_paisa, entry_type=LedgerEntryType.DEBIT.value, balance_after_paisa=sender.balance_paisa),
                LedgerEntry(transaction_id=transaction.id, user_id=receiver.id, amount_paisa=amount_paisa, entry_type=LedgerEntryType.CREDIT.value, balance_after_paisa=receiver.balance_paisa),
            ])
            result = _finish_transaction(transaction, commit=commit)
        log_audit_event("transfer_completed", user_id=sender_id, details={"reference": result.reference, "recipient_id": receiver_probe.id, "amount_paisa": amount_paisa}, commit=commit)
        return result
    except Exception:
        db.session.rollback()
        raise


def pay_merchant(user_id: int, merchant_id: int, amount: str | float | Decimal, *, is_paisa: bool = False, commit: bool = True) -> Transaction:
    amount_paisa = _parse_amount(amount, is_paisa=is_paisa, max_amount=MAX_TRANSFER_AMOUNT_PAISA, label="Payment")
    try:
        merchant = db.session.query(Merchant).filter_by(id=merchant_id).first()
        if not merchant:
            raise InvalidRecipientError(f"Merchant with ID {merchant_id} was not found.")
        if merchant.status != MerchantStatus.ACTIVE.value:
            raise InvalidRecipientError(f"Merchant '{merchant.name}' is inactive (status: {merchant.status}).")
        payer_probe = db.session.query(User.id).filter_by(id=user_id).first()
        if not payer_probe:
            raise InvalidRecipientError(f"User with ID {user_id} does not exist.")
        if merchant.owner_user_id == user_id:
            raise InvalidRecipientError("Cannot make a merchant payment to your own merchant account.")

        with _operation_lock([user_id, merchant.owner_user_id]):
            first_id, second_id = sorted([user_id, merchant.owner_user_id])
            first = db.session.query(User).filter_by(id=first_id).with_for_update().first()
            second = db.session.query(User).filter_by(id=second_id).with_for_update().first()
            payer = first if first.id == user_id else second
            owner = first if first.id == merchant.owner_user_id else second
            if not payer or not owner:
                raise InvalidRecipientError("Payer or merchant owner account could not be found.")
            if not payer.is_active_account() or not owner.is_active_account():
                raise AccountStatusError("Payer and merchant owner accounts must both be active.")

            daily_sent = _get_daily_sent_paisa(payer.id)
            if daily_sent + amount_paisa > MAX_DAILY_TRANSFER_PAISA:
                raise LimitExceededError(
                    f"Daily spending limit of {format_npr(MAX_DAILY_TRANSFER_PAISA)} exceeded. Today spent: {format_npr(daily_sent)}."
                )
            if payer.balance_paisa < amount_paisa:
                raise InsufficientBalanceError(
                    f"Insufficient balance. Available: {format_npr(payer.balance_paisa)}, Required: {format_npr(amount_paisa)}"
                )

            payer.balance_paisa -= amount_paisa
            owner.balance_paisa += amount_paisa
            transaction = Transaction(
                sender_id=payer.id, receiver_id=owner.id, merchant_id=merchant.id, merchant_name=merchant.name,
                type=TransactionType.PAID.value, amount_paisa=amount_paisa, currency=DEFAULT_CURRENCY,
                status=TransactionStatus.COMPLETED.value,
            )
            db.session.add(transaction)
            db.session.flush()
            db.session.add_all([
                LedgerEntry(transaction_id=transaction.id, user_id=payer.id, amount_paisa=amount_paisa, entry_type=LedgerEntryType.DEBIT.value, balance_after_paisa=payer.balance_paisa),
                LedgerEntry(transaction_id=transaction.id, user_id=owner.id, amount_paisa=amount_paisa, entry_type=LedgerEntryType.CREDIT.value, balance_after_paisa=owner.balance_paisa),
            ])
            result = _finish_transaction(transaction, commit=commit)
        log_audit_event("merchant_payment_completed", user_id=user_id, details={"reference": result.reference, "merchant_id": merchant.id, "amount_paisa": amount_paisa}, commit=commit)
        return result
    except Exception:
        db.session.rollback()
        raise


def topup_wallet(user_id: int, amount: str | float | Decimal, *, is_paisa: bool = False, commit: bool = True) -> Transaction:
    amount_paisa = _parse_amount(amount, is_paisa=is_paisa, max_amount=MAX_TOPUP_AMOUNT_PAISA, label="Top-up")
    try:
        with _operation_lock([user_id]):
            user = db.session.query(User).filter_by(id=user_id).with_for_update().first()
            if not user:
                raise InvalidRecipientError(f"User with ID {user_id} does not exist.")
            if not user.is_active_account():
                raise AccountStatusError(f"User account status is '{user.status}'. Cannot perform top-ups.")

            daily_topup = _get_daily_topup_paisa(user.id)
            if daily_topup + amount_paisa > MAX_DAILY_TOPUP_PAISA:
                raise LimitExceededError(
                    f"Daily top-up limit of {format_npr(MAX_DAILY_TOPUP_PAISA)} exceeded. Today credited: {format_npr(daily_topup)}."
                )
            user.balance_paisa += amount_paisa
            transaction = Transaction(
                receiver_id=user.id, type=TransactionType.TOPUP.value, amount_paisa=amount_paisa,
                currency=DEFAULT_CURRENCY, status=TransactionStatus.COMPLETED.value,
            )
            db.session.add(transaction)
            db.session.flush()
            db.session.add(LedgerEntry(
                transaction_id=transaction.id, user_id=user.id, amount_paisa=amount_paisa,
                entry_type=LedgerEntryType.CREDIT.value, balance_after_paisa=user.balance_paisa,
            ))
            result = _finish_transaction(transaction, commit=commit)
        log_audit_event("wallet_topup_completed", user_id=user_id, details={"reference": result.reference, "amount_paisa": amount_paisa, "simulated": True}, commit=commit)
        return result
    except Exception:
        db.session.rollback()
        raise


def reverse_transaction(transaction_id: int, *, actor_user_id: int, reason: str = "Administrative correction", commit: bool = True) -> Transaction:
    """Atomically reverse a completed transaction exactly once; only admins may invoke it."""
    try:
        actor = db.session.query(User).filter_by(id=actor_user_id).first()
        if not actor or actor.role != UserRole.ADMIN.value:
            raise AdminAuthorizationError()
        orig = db.session.query(Transaction).filter_by(id=transaction_id).with_for_update().first()
        if not orig:
            raise WalletError(f"Transaction #{transaction_id} not found.")
        if orig.status == TransactionStatus.REVERSED.value and orig.reversal:
            return orig.reversal
        if orig.status != TransactionStatus.COMPLETED.value:
            raise WalletError(f"Cannot reverse transaction in '{orig.status}' state.")

        user_ids = [x for x in (orig.sender_id, orig.receiver_id) if x]
        with _operation_lock(user_ids):
            # Re-read locked account rows in deterministic order.
            locked_users = {}
            for uid in sorted(set(user_ids)):
                locked_users[uid] = db.session.query(User).filter_by(id=uid).with_for_update().first()

            if orig.type in (TransactionType.SENT.value, TransactionType.PAID.value):
                sender = locked_users.get(orig.sender_id)
                receiver = locked_users.get(orig.receiver_id)
                if not sender or not receiver:
                    raise WalletError("Original transaction account is missing.")
                if receiver.balance_paisa < orig.amount_paisa:
                    raise InsufficientBalanceError("Receiving account has insufficient balance to complete reversal.")
                receiver.balance_paisa -= orig.amount_paisa
                sender.balance_paisa += orig.amount_paisa
                reversal = Transaction(
                    sender_id=receiver.id, receiver_id=sender.id, merchant_id=orig.merchant_id,
                    merchant_name=orig.merchant_name, type=TransactionType.REVERSAL.value,
                    amount_paisa=orig.amount_paisa, currency=orig.currency, status=TransactionStatus.COMPLETED.value,
                    reversed_transaction_id=orig.id,
                )
                db.session.add(reversal)
                db.session.flush()
                db.session.add_all([
                    LedgerEntry(transaction_id=reversal.id, user_id=receiver.id, amount_paisa=orig.amount_paisa, entry_type=LedgerEntryType.DEBIT.value, balance_after_paisa=receiver.balance_paisa),
                    LedgerEntry(transaction_id=reversal.id, user_id=sender.id, amount_paisa=orig.amount_paisa, entry_type=LedgerEntryType.CREDIT.value, balance_after_paisa=sender.balance_paisa),
                ])
            elif orig.type == TransactionType.TOPUP.value:
                receiver = locked_users.get(orig.receiver_id)
                if not receiver:
                    raise WalletError("Original top-up account is missing.")
                if receiver.balance_paisa < orig.amount_paisa:
                    raise InsufficientBalanceError("Wallet balance is insufficient to reverse the simulated top-up.")
                receiver.balance_paisa -= orig.amount_paisa
                reversal = Transaction(
                    sender_id=receiver.id, receiver_id=None, merchant_id=None, merchant_name=None,
                    type=TransactionType.REVERSAL.value, amount_paisa=orig.amount_paisa, currency=orig.currency,
                    status=TransactionStatus.COMPLETED.value, reversed_transaction_id=orig.id,
                )
                db.session.add(reversal)
                db.session.flush()
                db.session.add(LedgerEntry(
                    transaction_id=reversal.id, user_id=receiver.id, amount_paisa=orig.amount_paisa,
                    entry_type=LedgerEntryType.DEBIT.value, balance_after_paisa=receiver.balance_paisa,
                ))
            else:
                raise WalletError(f"Reversal for transaction type '{orig.type}' is not supported.")

            orig.status = TransactionStatus.REVERSED.value
            _finish_transaction(reversal, commit=commit)

        log_audit_event("transaction_reversed", user_id=actor_user_id, details={"reference": orig.reference, "reversal_reference": reversal.reference, "reason": reason}, commit=commit)
        return reversal
    except Exception:
        db.session.rollback()
        raise
