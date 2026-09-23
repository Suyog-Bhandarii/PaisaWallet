"""
Audit Logging Module for Paisa Wallet.
Safely logs security and financial lifecycle events to the database and logger.
"""
import json
import logging
from typing import Any

from flask import has_request_context, request

logger = logging.getLogger("paisa_wallet.audit")


def log_audit_event(
    event_type: str,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    *,
    commit: bool = True,
) -> None:
    """
    Records an audit log entry.
    Strictly filters out passwords, keys, tokens, and secret fields.
    """
    from models import AuditLog, db

    # Sanitize details
    sanitized: dict[str, Any] = {}
    if details:
        disallowed_keys = {"password", "confirm_password", "secret", "token", "csrf_token", "api_key"}
        for k, v in details.items():
            if any(bad in k.lower() for bad in disallowed_keys):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v

    # request.remote_addr is already normalized by ProxyFix when a trusted proxy count
    # is explicitly configured. Never consume X-Forwarded-For directly from clients.
    if not ip_address and has_request_context():
        ip_address = request.remote_addr

    details_str = json.dumps(sanitized) if sanitized else None

    try:
        entry = AuditLog(
            user_id=user_id,
            event_type=event_type,
            ip_address=ip_address,
            details=details_str
        )
        db.session.add(entry)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception as e:
        logger.error(f"Failed to record audit log for event {event_type}: {e}")
        if commit:
            try:
                db.session.rollback()
            except Exception as rb_err:
                logger.debug(f"Audit log rollback suppressed: {rb_err}")
        else:
            try:
                db.session.expunge(entry)
            except Exception:
                pass
