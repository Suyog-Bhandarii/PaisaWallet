"""Explicit application enums used by wallet/accounting workflows."""
from enum import Enum


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class MerchantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class TransactionType(str, Enum):
    SENT = "sent"
    PAID = "paid"
    TOPUP = "topup"
    REVERSAL = "reversal"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERSED = "reversed"


class LedgerEntryType(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class IdempotencyState(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
