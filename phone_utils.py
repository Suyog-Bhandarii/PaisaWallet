"""
Nepal Mobile Number Normalization and Validation Utility.
Accepts variations like +977-9841234567, 9779841234567, 09841234567, and 9841234567.
Normalizes to canonical 10-digit format: 98XXXXXXXX or 97XXXXXXXX.
"""
import re


class InvalidPhoneError(ValueError):
    """Raised when a phone number is not a valid Nepal mobile number."""


NEPAL_PHONE_REGEX = re.compile(r"^(?:98|97)\d{8}$")


def normalize_nepal_phone(phone_input: str) -> str:
    """
    Normalizes and validates a Nepal mobile phone number.
    Returns:
        Canonical 10-digit string starting with 98 or 97.
    Raises:
        InvalidPhoneError: If phone number is empty, malformed, or not a valid Nepali mobile.
    """
    if not phone_input or not isinstance(phone_input, (str, int)):
        raise InvalidPhoneError("Phone number cannot be empty.")

    raw = str(phone_input).strip()

    # Remove all formatting characters (spaces, dashes, parens, dots)
    cleaned = re.sub(r"[\s\-\(\)\.]", "", raw)

    # Remove international prefix +977 or 977
    if cleaned.startswith("+977"):
        cleaned = cleaned[4:]
    elif cleaned.startswith("977") and len(cleaned) == 13:
        cleaned = cleaned[3:]

    # Remove single leading zero (e.g. 09841000000)
    if cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]

    # Must match 10 digits starting with 98 or 97 (NTC, Ncell, SmartCell prefixes)
    if not NEPAL_PHONE_REGEX.match(cleaned):
        raise InvalidPhoneError(
            f"Invalid Nepal mobile number '{raw}'. Must be 10 digits starting with 98 or 97."
        )

    return cleaned
