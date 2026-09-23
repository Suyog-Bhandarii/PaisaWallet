"""
Request validation and standardized error formatting for Paisa Wallet APIs.
"""
from typing import Any

from flask import Response, jsonify

from money import InvalidAmountError, format_npr, validate_amount_paisa
from phone_utils import InvalidPhoneError, normalize_nepal_phone


class ValidationError(ValueError):
    """Raised when an API request payload fails validation."""
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def api_error(code: str, message: str, status_code: int = 400) -> tuple[Response, int]:
    """
    Standardized API error response format (Phase 12).
    Provides:
    {
        "status": "error",
        "error": {
            "code": code,
            "message": message
        },
        "code": code
    }
    """
    return jsonify({
        "status": "error",
        "code": code,
        "error": {
            "code": code,
            "message": message
        },
        "message": message
    }), status_code


def validate_send_payload(data: Any, max_amount_paisa: int = 5000000) -> tuple[str, int]:
    """
    Validates payload for POST /api/send: { "phone": "...", "amount": ... }
    Returns (normalized_phone, amount_paisa).
    """
    if not isinstance(data, dict):
        raise ValidationError("invalid_payload", "Request payload must be a valid JSON object.")

    phone_raw = data.get("phone") or data.get("phone_number")
    if not phone_raw:
        raise ValidationError("missing_field", "Recipient phone number is required.")

    try:
        clean_phone = normalize_nepal_phone(str(phone_raw))
    except InvalidPhoneError as e:
        raise ValidationError("invalid_recipient", str(e))

    amount_raw = data.get("amount")
    if amount_raw is None:
        raise ValidationError("missing_field", "Transfer amount is required.")

    try:
        amount_paisa = validate_amount_paisa(amount_raw, min_paisa=100)
    except InvalidAmountError as e:
        raise ValidationError("invalid_amount", str(e))

    if max_amount_paisa is not None and amount_paisa > max_amount_paisa:
        raise ValidationError(
            "limit_exceeded",
            f"Transfer amount exceeds single-transaction limit of {format_npr(max_amount_paisa)}."
        )

    return clean_phone, amount_paisa


def validate_pay_payload(data: Any, max_amount_paisa: int = 5000000) -> tuple[int, int]:
    """
    Validates payload for POST /api/pay: { "merchant_id": 1, "amount": ... }
    Returns (merchant_id_int, amount_paisa).
    """
    if not isinstance(data, dict):
        raise ValidationError("invalid_payload", "Request payload must be a valid JSON object.")

    merchant_id_raw = data.get("merchant_id")
    if merchant_id_raw is None:
        raise ValidationError("missing_field", "merchant_id is required.")

    try:
        merchant_id = int(merchant_id_raw)
        if merchant_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValidationError("invalid_parameter", "merchant_id must be a positive integer.")

    amount_raw = data.get("amount")
    if amount_raw is None:
        raise ValidationError("missing_field", "Payment amount is required.")

    try:
        amount_paisa = validate_amount_paisa(amount_raw, min_paisa=100)
    except InvalidAmountError as e:
        raise ValidationError("invalid_amount", str(e))

    if max_amount_paisa is not None and amount_paisa > max_amount_paisa:
        raise ValidationError(
            "limit_exceeded",
            f"Payment amount exceeds maximum limit of {format_npr(max_amount_paisa)}."
        )

    return merchant_id, amount_paisa


def validate_topup_payload(data: Any, max_amount_paisa: int = 2500000) -> int:
    """
    Validates payload for POST /api/topup: { "amount": ... }
    Returns amount_paisa.
    """
    if not isinstance(data, dict):
        raise ValidationError("invalid_payload", "Request payload must be a valid JSON object.")

    amount_raw = data.get("amount")
    if amount_raw is None:
        raise ValidationError("missing_field", "Top-up amount is required.")

    try:
        amount_paisa = validate_amount_paisa(amount_raw, min_paisa=100)
    except InvalidAmountError as e:
        raise ValidationError("invalid_amount", str(e))

    if max_amount_paisa is not None and amount_paisa > max_amount_paisa:
        raise ValidationError(
            "limit_exceeded",
            f"Top-up amount exceeds maximum single top-up limit of {format_npr(max_amount_paisa)}."
        )

    return amount_paisa



def validate_signup_payload(data: Any) -> tuple[str, str, str]:
    """
    Validates payload for signup: name, phone_number, password.
    Returns (clean_name, clean_phone, password).
    """
    if not isinstance(data, dict):
        raise ValidationError("invalid_payload", "Request payload must be a valid form or JSON object.")

    name = (data.get("name") or "").strip()
    phone_raw = (data.get("phone_number") or data.get("phone") or "").strip()
    password = data.get("password") or ""

    if not name:
        raise ValidationError("missing_field", "Full name is required.")
    if len(name) < 2 or len(name) > 100:
        raise ValidationError("invalid_field", "Name must be between 2 and 100 characters.")

    if not phone_raw:
        raise ValidationError("missing_field", "Nepal mobile number is required.")
    try:
        clean_phone = normalize_nepal_phone(phone_raw)
    except InvalidPhoneError as e:
        raise ValidationError("invalid_phone", str(e))

    if not password:
        raise ValidationError("missing_field", "Password is required.")
    if len(password) < 8:
        raise ValidationError("weak_password", "Password must be at least 8 characters long.")

    return name, clean_phone, password


def validate_merchant_payload(data: Any) -> tuple[str, str]:
    """
    Validates payload for merchant registration: business name and optional emoji.
    Returns (clean_name, clean_emoji).
    """
    if not isinstance(data, dict):
        raise ValidationError("invalid_payload", "Request payload must be a valid form or JSON object.")

    name = (data.get("name") or "").strip()
    emoji = (data.get("emoji") or "🏪").strip()

    if not name:
        raise ValidationError("missing_field", "Merchant business name is required.")
    if len(name) < 2 or len(name) > 100:
        raise ValidationError("invalid_field", "Business name must be between 2 and 100 characters.")

    return name, emoji[:10] if emoji else "🏪"

