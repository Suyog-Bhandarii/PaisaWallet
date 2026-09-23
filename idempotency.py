"""Transactional idempotency reservation and response replay support."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from enums import IdempotencyState
from models import IdempotencyRecord, db

IDEMPOTENCY_TTL_HOURS = 24


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different payload."""


class IdempotencyInProgressError(Exception):
    """Raised only if a matching request is still being processed."""


def compute_request_hash(data: Any) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_idempotency_key() -> str | None:
    key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    if not key:
        return None
    clean = key.strip()
    if not clean:
        return None
    return clean[:128]


def find_idempotency_record(user_id: int, key: str, operation: str) -> IdempotencyRecord | None:
    return IdempotencyRecord.query.filter_by(user_id=user_id, key=key, operation=operation).first()


def reserve_idempotency_record(
    user_id: int,
    key: str,
    operation: str,
    request_hash: str,
) -> tuple[IdempotencyRecord, bool]:
    """Atomically reserve a key before financial execution.

    The unique constraint is the serialization point. PostgreSQL blocks a concurrent
    duplicate insert until the first transaction commits, after which the caller reads
    the completed record. A nested transaction prevents the uniqueness error from
    poisoning the outer transaction.
    """
    try:
        with db.session.begin_nested():
            now = datetime.now(timezone.utc)
            db.session.execute(delete(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.key == key,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.expires_at < now,
            ))
            record = IdempotencyRecord(
                user_id=user_id,
                key=key,
                operation=operation,
                request_hash=request_hash,
                state=IdempotencyState.PROCESSING.value,
                response_code=None,
                response_body=None,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
            )
            db.session.add(record)
            db.session.flush()
        return record, True
    except IntegrityError:
        existing = find_idempotency_record(user_id, key, operation)
        if not existing:
            raise
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError("Idempotency key reused with conflicting request payload.")
        if existing.state == IdempotencyState.COMPLETED.value and existing.response_body:
            return existing, False
        raise IdempotencyInProgressError("A request with this idempotency key is already being processed.")


def complete_idempotency_record(
    record: IdempotencyRecord,
    response_code: int,
    response_body: dict[str, Any],
) -> None:
    record.state = IdempotencyState.COMPLETED.value
    record.response_code = response_code
    record.response_body = json.dumps(response_body, sort_keys=True)
    db.session.flush()


def fail_idempotency_record(record: IdempotencyRecord) -> None:
    """Mark a reservation failed; the surrounding financial transaction should roll back."""
    record.state = IdempotencyState.FAILED.value
    db.session.flush()
