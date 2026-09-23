"""
Money and Currency Module for Paisa Wallet.

Enforces integer-paisa financial mathematics with zero float calculations.
1 NPR = 100 paisa.

Design:
- Transaction/payment amounts must be strictly greater than zero.
- Wallet/account balances may be zero, but never negative.
- Monetary values are stored as integer paisa.
"""

from decimal import Decimal, InvalidOperation


DEFAULT_CURRENCY = "NPR"
PAISA_PER_NPR = 100


class MoneyError(ValueError):
    """Base exception for money parsing and validation errors."""


class InvalidAmountError(MoneyError):
    """
    Raised when a monetary amount is invalid.

    This includes negative/zero transaction amounts, malformed values,
    excessive precision, or non-finite values.
    """


def paisa_to_npr(amount_paisa: int) -> Decimal:
    """
    Convert integer paisa to Decimal NPR representation.

    Example:
        500000 paisa -> Decimal("5000.00")
        0 paisa      -> Decimal("0.00")
    """
    if not isinstance(amount_paisa, int) or isinstance(amount_paisa, bool):
        raise MoneyError(
            f"Paisa amount must be an integer. "
            f"Received: {type(amount_paisa).__name__}"
        )

    return (
        Decimal(amount_paisa) / Decimal(PAISA_PER_NPR)
    ).quantize(Decimal("0.01"))


def _parse_npr_decimal(
    amount_npr: str | int | float | Decimal,
) -> Decimal:
    """
    Parse an NPR input into a Decimal without applying
    positive/zero balance rules.

    This helper performs only format and precision validation.
    """
    if amount_npr is None:
        raise InvalidAmountError("Amount cannot be None.")

    if isinstance(amount_npr, bool):
        raise InvalidAmountError("Amount cannot be a boolean value.")

    # Convert to string for strict Decimal parsing.
    str_val = str(amount_npr).strip()

    if not str_val:
        raise InvalidAmountError("Amount string cannot be empty.")

    # Disallow scientific notation, Infinity, and NaN.
    lowered = str_val.lower()

    if any(value in lowered for value in ("inf", "nan", "e")):
        raise InvalidAmountError(
            f"Invalid numeric format: '{str_val}'."
        )

    try:
        dec = Decimal(str_val)
    except (InvalidOperation, ValueError):
        raise InvalidAmountError(
            f"Cannot parse '{str_val}' into a valid monetary amount."
        )

    if not dec.is_finite():
        raise InvalidAmountError(
            "Amount must be a finite number."
        )

    # Maximum 2 decimal places.
    #
    # Examples:
    #   10       -> exponent 0
    #   10.5     -> exponent -1
    #   10.55    -> exponent -2
    #   10.555   -> exponent -3 -> reject
    exponent = dec.as_tuple().exponent

    if isinstance(exponent, int) and exponent < -2:
        raise InvalidAmountError(
            f"Amount '{str_val}' has more than 2 decimal places. "
            "Silent rounding is forbidden."
        )

    return dec


def npr_to_paisa(
    amount_npr: str | int | float | Decimal,
) -> int:
    """
    Convert an NPR transaction amount into integer paisa.

    Transaction amounts must be strictly positive.

    Rejects:
    - None
    - empty strings
    - booleans
    - negative amounts
    - zero
    - non-numeric strings
    - NaN
    - Infinity
    - scientific notation
    - more than 2 decimal places

    Examples:
        "100"    -> 10000
        "100.50" -> 10050

        "0"      -> raises InvalidAmountError
        "-10"    -> raises InvalidAmountError
    """
    dec = _parse_npr_decimal(amount_npr)

    if dec <= Decimal("0"):
        raise InvalidAmountError(
            f"Amount must be strictly positive (> 0). "
            f"Received: {dec}"
        )

    # Convert precisely to integer paisa.
    paisa_val = int(dec * PAISA_PER_NPR)

    # Defensive check. This should never fail because precision
    # has already been restricted to 2 decimal places.
    if paisa_val <= 0:
        raise InvalidAmountError(
            f"Amount must be strictly positive (> 0). "
            f"Received: {dec}"
        )

    return paisa_val


def npr_balance_to_paisa(
    amount_npr: str | int | float | Decimal,
) -> int:
    """
    Convert a wallet/account balance into integer paisa.

    Balances may legitimately be zero.

    Allowed:
        "0"       -> 0
        "0.00"    -> 0
        "100"     -> 10000
        "100.50"  -> 10050

    Rejected:
        negative values
        malformed values
        NaN
        Infinity
        scientific notation
        more than 2 decimal places
    """
    dec = _parse_npr_decimal(amount_npr)

    if dec < Decimal("0"):
        raise InvalidAmountError(
            f"Balance cannot be negative. Received: {dec}"
        )

    # Convert precisely to integer paisa.
    paisa_val = int(dec * PAISA_PER_NPR)

    if paisa_val < 0:
        raise InvalidAmountError(
            f"Balance cannot be negative. Received: {dec}"
        )

    return paisa_val


def validate_amount_paisa(
    amount_input: str | int | float | Decimal,
    min_paisa: int = 1,
    max_paisa: int | None = None,
) -> int:
    """
    Validate and convert a transaction amount into integer paisa.

    The default minimum is 1 paisa, so zero is rejected.

    Args:
        amount_input:
            User-provided NPR amount.

        min_paisa:
            Minimum allowed amount in paisa.

        max_paisa:
            Optional maximum allowed amount in paisa.

    Returns:
        Integer paisa.

    Raises:
        InvalidAmountError:
            If the amount is invalid or outside the allowed range.
    """
    if not isinstance(min_paisa, int) or isinstance(min_paisa, bool):
        raise InvalidAmountError(
            "min_paisa must be an integer."
        )

    if min_paisa < 0:
        raise InvalidAmountError(
            "min_paisa cannot be negative."
        )

    if max_paisa is not None:
        if not isinstance(max_paisa, int) or isinstance(max_paisa, bool):
            raise InvalidAmountError(
                "max_paisa must be an integer."
            )

        if max_paisa < 0:
            raise InvalidAmountError(
                "max_paisa cannot be negative."
            )

        if max_paisa < min_paisa:
            raise InvalidAmountError(
                "max_paisa cannot be less than min_paisa."
            )

    paisa = npr_to_paisa(amount_input)

    if paisa < min_paisa:
        min_npr = paisa_to_npr(min_paisa)

        raise InvalidAmountError(
            f"Amount must be at least NPR {min_npr:.2f}."
        )

    if max_paisa is not None and paisa > max_paisa:
        max_npr = paisa_to_npr(max_paisa)

        raise InvalidAmountError(
            f"Amount cannot exceed NPR {max_npr:.2f}."
        )

    return paisa


def format_npr(amount_paisa: int) -> str:
    """
    Format integer paisa into standard display format.

    Examples:
        500000 -> "NPR 5,000.00"
        0      -> "NPR 0.00"
    """
    dec = paisa_to_npr(amount_paisa)

    return f"NPR {dec:,.2f}"
