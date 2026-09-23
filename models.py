"""SQLAlchemy models and database invariants for Paisa Wallet."""

import secrets

from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, event

from enums import (
    AccountStatus,
    IdempotencyState,
    LedgerEntryType,
    MerchantStatus,
    TransactionStatus,
    TransactionType,
    UserRole,
)

from money import (
    npr_balance_to_paisa,
    npr_to_paisa,
    paisa_to_npr,
)


db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(
        db.String(100),
        nullable=False,
    )

    phone_number = db.Column(
        db.String(20),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    # All persisted financial values are stored as integer paisa.
    balance_paisa = db.Column(
        db.BigInteger,
        default=0,
        nullable=False,
    )

    currency = db.Column(
        db.String(3),
        default="NPR",
        nullable=False,
    )

    status = db.Column(
        db.String(20),
        default=AccountStatus.ACTIVE.value,
        nullable=False,
        index=True,
    )

    role = db.Column(
        db.String(20),
        default=UserRole.USER.value,
        nullable=False,
        index=True,
    )

    is_merchant = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
    )

    failed_login_attempts = db.Column(
        db.Integer,
        default=0,
        nullable=False,
    )

    locked_until = db.Column(
        db.DateTime,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "balance_paisa >= 0",
            name="ck_user_balance_positive",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','CLOSED')",
            name="ck_user_status",
        ),
        CheckConstraint(
            "role IN ('USER','ADMIN')",
            name="ck_user_role",
        ),
    )

    sent_transactions = db.relationship(
        "Transaction",
        foreign_keys="Transaction.sender_id",
        backref="sender",
        lazy=True,
    )

    received_transactions = db.relationship(
        "Transaction",
        foreign_keys="Transaction.receiver_id",
        backref="receiver",
        lazy=True,
    )

    merchants = db.relationship(
        "Merchant",
        backref="owner",
        lazy=True,
    )

    @property
    def balance(self) -> float:
        """
        Legacy presentation property.

        Persisted/accounting values remain integer paisa.
        """
        return float(
            paisa_to_npr(self.balance_paisa or 0)
        )

    @balance.setter
    def balance(self, value):
        """
        Set wallet balance from an NPR value.

        Wallet balances are allowed to be zero but never negative.

        This intentionally does NOT use npr_to_paisa(), because
        npr_to_paisa() is intentionally strict and rejects zero
        transaction amounts.
        """
        if value is None:
            self.balance_paisa = 0
            return

        self.balance_paisa = npr_balance_to_paisa(value)

    def set_password(self, bcrypt, password: str) -> None:
        self.password_hash = bcrypt.generate_password_hash(
            password
        ).decode("utf-8")

    def check_password(self, bcrypt, password: str) -> bool:
        return bcrypt.check_password_hash(
            self.password_hash,
            password,
        )

    def is_active_account(self) -> bool:
        if self.status != AccountStatus.ACTIVE.value:
            return False

        if self.locked_until:
            now = datetime.now(timezone.utc)

            locked_until_utc = self.locked_until

            if locked_until_utc.tzinfo is None:
                locked_until_utc = locked_until_utc.replace(
                    tzinfo=timezone.utc
                )

            return now >= locked_until_utc

        return True

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "phone_number": self.phone_number,
            "balance_paisa": self.balance_paisa,
            "balance": self.balance,
            "currency": self.currency,
            "status": self.status,
            "role": self.role,
            "is_merchant": self.is_merchant,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }


class Merchant(db.Model):
    __tablename__ = "merchants"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    name = db.Column(
        db.String(100),
        nullable=False,
    )

    emoji = db.Column(
        db.String(10),
        nullable=True,
    )

    qr_code_id = db.Column(
        db.String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(20),
        default=MerchantStatus.ACTIVE.value,
        nullable=False,
        index=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','REVOKED')",
            name="ck_merchant_status",
        ),
    )

    transactions = db.relationship(
        "Transaction",
        backref="merchant",
        lazy=True,
    )

    @classmethod
    def generate_qr_id(cls) -> str:
        return f"qr_sec_{secrets.token_urlsafe(12)}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "qr_code_id": self.qr_code_id,
            "owner_user_id": self.owner_user_id,
            "status": self.status,
        }


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    reference = db.Column(
        db.String(40),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: Transaction.generate_reference(),
    )

    sender_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    receiver_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    merchant_id = db.Column(
        db.Integer,
        db.ForeignKey("merchants.id"),
        nullable=True,
        index=True,
    )

    merchant_name = db.Column(
        db.String(100),
        nullable=True,
    )

    type = db.Column(
        db.String(20),
        nullable=False,
        index=True,
    )

    amount_paisa = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
    )

    currency = db.Column(
        db.String(3),
        default="NPR",
        nullable=False,
    )

    timestamp = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(20),
        default=TransactionStatus.COMPLETED.value,
        nullable=False,
        index=True,
    )

    reversed_transaction_id = db.Column(
        db.Integer,
        db.ForeignKey("transactions.id"),
        nullable=True,
        unique=True,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "amount_paisa > 0",
            name="ck_transaction_amount_positive",
        ),
        CheckConstraint(
            "type IN ('sent','paid','topup','reversal')",
            name="ck_transaction_type",
        ),
        CheckConstraint(
            "status IN ('pending','completed','failed','reversed')",
            name="ck_transaction_status",
        ),
        Index(
            "ix_txn_sender_ts",
            "sender_id",
            "timestamp",
        ),
        Index(
            "ix_txn_receiver_ts",
            "receiver_id",
            "timestamp",
        ),
        Index(
            "ix_txn_merchant_ts",
            "merchant_id",
            "timestamp",
        ),
    )

    reversal_of = db.relationship(
        "Transaction",
        remote_side=[id],
        uselist=False,
        backref=db.backref(
            "reversal",
            uselist=False,
        ),
    )

    @classmethod
    def generate_reference(cls) -> str:
        return f"TXN-{secrets.token_hex(8).upper()}"

    @property
    def amount(self) -> float:
        return float(
            paisa_to_npr(self.amount_paisa or 0)
        )

    @amount.setter
    def amount(self, value):
        """
        Transaction amounts must be strictly positive.
        """
        self.amount_paisa = npr_to_paisa(value)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "reference": self.reference,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "merchant_id": self.merchant_id,
            "merchant_name": self.merchant_name,
            "type": self.type,
            "amount_paisa": self.amount_paisa,
            "amount": self.amount,
            "currency": self.currency,
            "timestamp": (
                self.timestamp.isoformat()
                if self.timestamp
                else None
            ),
            "status": self.status,
            "reversed_transaction_id": self.reversed_transaction_id,
        }


class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"

    id = db.Column(
        db.BigInteger().with_variant(
            db.Integer,
            "sqlite",
        ),
        primary_key=True,
        autoincrement=True,
    )

    transaction_id = db.Column(
        db.Integer,
        db.ForeignKey("transactions.id"),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    amount_paisa = db.Column(
        db.BigInteger,
        nullable=False,
    )

    entry_type = db.Column(
        db.String(10),
        nullable=False,
    )

    balance_after_paisa = db.Column(
        db.BigInteger,
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "amount_paisa > 0",
            name="ck_ledger_amount_positive",
        ),
        CheckConstraint(
            "balance_after_paisa >= 0",
            name="ck_ledger_balance_after_nonnegative",
        ),
        CheckConstraint(
            "entry_type IN ('debit','credit')",
            name="ck_ledger_entry_type",
        ),
        Index(
            "ix_ledger_user_ts",
            "user_id",
            "created_at",
        ),
    )

    transaction = db.relationship(
        "Transaction",
        backref=db.backref(
            "ledger_entries",
            lazy=True,
        ),
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "ledger_entries",
            lazy=True,
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transaction_id": self.transaction_id,
            "user_id": self.user_id,
            "amount_paisa": self.amount_paisa,
            "entry_type": self.entry_type,
            "balance_after_paisa": self.balance_after_paisa,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }


class IdempotencyRecord(db.Model):
    __tablename__ = "idempotency_records"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    key = db.Column(
        db.String(128),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    operation = db.Column(
        db.String(50),
        nullable=False,
    )

    request_hash = db.Column(
        db.String(64),
        nullable=False,
    )

    state = db.Column(
        db.String(20),
        nullable=False,
        default=IdempotencyState.PROCESSING.value,
    )

    response_code = db.Column(
        db.Integer,
        nullable=True,
    )

    response_body = db.Column(
        db.Text,
        nullable=True,
    )

    expires_at = db.Column(
        db.DateTime,
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "key",
            "operation",
            name="uq_idempotency_user_key_op",
        ),
        CheckConstraint(
            "state IN ('processing','completed','failed')",
            name="ck_idempotency_state",
        ),
    )


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    user_id = db.Column(
        db.Integer,
        nullable=True,
        index=True,
    )

    event_type = db.Column(
        db.String(64),
        nullable=False,
        index=True,
    )

    ip_address = db.Column(
        db.String(45),
        nullable=True,
    )

    details = db.Column(
        db.Text,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )


@event.listens_for(LedgerEntry, "before_update")
def prevent_ledger_update(mapper, connection, target):
    """
    Ledger entries are immutable.

    Corrections must be represented by new ledger entries rather
    than modifying historical accounting records.
    """
    raise ValueError(
        "Ledger entries are immutable and cannot be updated."
    )


@event.listens_for(LedgerEntry, "before_delete")
def prevent_ledger_delete(mapper, connection, target):
    """
    Ledger entries are immutable and cannot be deleted.
    """
    raise ValueError(
        "Ledger entries are immutable and cannot be deleted."
    )


@event.listens_for(AuditLog, "before_update")
def prevent_audit_update(mapper, connection, target):
    """
    Audit logs are append-only.
    """
    raise ValueError(
        "Audit logs are append-only and cannot be updated."
    )


@event.listens_for(AuditLog, "before_delete")
def prevent_audit_delete(mapper, connection, target):
    """
    Audit logs are append-only and cannot be deleted.
    """
    raise ValueError(
        "Audit logs are append-only and cannot be deleted."
    )