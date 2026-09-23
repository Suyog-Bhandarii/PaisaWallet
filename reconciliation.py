"""
Ledger Reconciliation Utility for Paisa Wallet.
Audits user wallet balances against double-entry ledger records.
Detects any discrepancies without modifying records.
"""
import logging

from sqlalchemy import func

from models import LedgerEntry, User, db

logger = logging.getLogger("paisa_wallet.reconciliation")


def reconcile_user_balance(user_id: int) -> dict:
    """
    Reconciles a single user's wallet balance against their ledger history.
    Returns audit report dictionary with discrepancy details if any.
    """
    user = db.session.get(User, user_id)
    if not user:
        return {"status": "error", "error": f"User #{user_id} not found."}

    # Sum credits
    total_credits = db.session.query(func.coalesce(func.sum(LedgerEntry.amount_paisa), 0)).filter(
        LedgerEntry.user_id == user_id,
        LedgerEntry.entry_type == 'credit'
    ).scalar() or 0

    # Sum debits
    total_debits = db.session.query(func.coalesce(func.sum(LedgerEntry.amount_paisa), 0)).filter(
        LedgerEntry.user_id == user_id,
        LedgerEntry.entry_type == 'debit'
    ).scalar() or 0

    expected_balance_paisa = int(total_credits) - int(total_debits)
    actual_balance_paisa = int(user.balance_paisa)
    discrepancy = actual_balance_paisa - expected_balance_paisa

    is_matched = (discrepancy == 0)

    report = {
        "user_id": user_id,
        "user_name": user.name,
        "phone_number": user.phone_number,
        "actual_balance_paisa": actual_balance_paisa,
        "expected_balance_paisa": expected_balance_paisa,
        "total_credits_paisa": total_credits,
        "total_debits_paisa": total_debits,
        "discrepancy_paisa": discrepancy,
        "status": "MATCHED" if is_matched else "DISCREPANCY_DETECTED"
    }

    if not is_matched:
        logger.error(
            f"RECONCILIATION MISMATCH: User #{user_id} ({user.phone_number}). "
            f"Actual: {actual_balance_paisa} paisa, Expected: {expected_balance_paisa} paisa. "
            f"Discrepancy: {discrepancy} paisa."
        )

    return report


def run_full_reconciliation() -> list[dict]:
    """
    Audits all users in the system and returns list of reconciliation reports.
    """
    users = User.query.all()
    reports = []
    for u in users:
        reports.append(reconcile_user_balance(u.id))
    return reports


if __name__ == "__main__":
    from app import create_app
    app = create_app()
    with app.app_context():
        results = run_full_reconciliation()
        mismatches = [r for r in results if r["status"] != "MATCHED"]
        print(f"Reconciliation completed for {len(results)} accounts. Mismatches: {len(mismatches)}")
        for m in mismatches:
            print(f"  ALERT: User #{m['user_id']} ({m['phone_number']}) discrepancy: {m['discrepancy_paisa']} paisa")


def validate_ledger_invariants(transaction_id: int) -> dict:
    """Validate the ledger shape for one transaction without changing data."""
    from enums import LedgerEntryType, TransactionType
    from models import Transaction

    tx = db.session.get(Transaction, transaction_id)
    if not tx:
        return {"status": "ERROR", "error": f"Transaction #{transaction_id} not found."}
    entries = list(tx.ledger_entries)
    debit_total = sum(e.amount_paisa for e in entries if e.entry_type == LedgerEntryType.DEBIT.value)
    credit_total = sum(e.amount_paisa for e in entries if e.entry_type == LedgerEntryType.CREDIT.value)
    if tx.type in (TransactionType.SENT.value, TransactionType.PAID.value):
        matched = len(entries) == 2 and debit_total == tx.amount_paisa and credit_total == tx.amount_paisa
    elif tx.type == TransactionType.TOPUP.value:
        matched = len(entries) == 1 and credit_total == tx.amount_paisa
    elif tx.type == TransactionType.REVERSAL.value:
        matched = tx.reversed_transaction_id is not None and debit_total == tx.amount_paisa
    else:
        matched = False
    return {
        "status": "VALID" if matched else "INVALID",
        "transaction_id": tx.id,
        "reference": tx.reference,
        "entry_count": len(entries),
        "debit_total_paisa": debit_total,
        "credit_total_paisa": credit_total,
        "amount_paisa": tx.amount_paisa,
    }
