"""
Manual test suite for wallet_service.py
Validates transfer_funds and pay_merchant functions, atomic rollback, and exception handling.
"""
import io
import sys

# Ensure UTF-8 output on Windows consoles
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from app import bcrypt, create_app
from config import Config
from models import Merchant, User, db
from wallet_service import (
    InsufficientBalanceError,
    InvalidAmountError,
    InvalidRecipientError,
    pay_merchant,
    transfer_funds,
)


def run_tests():
    class TestConfig(Config):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
        WTF_CSRF_ENABLED = False

    app = create_app(TestConfig)

    with app.app_context():
        db.create_all()
        print("=" * 60)
        print("PAISA WALLET SERVICE - MANUAL TEST RUNNER")
        print("=" * 60)

        # Setup test entities
        alice = User(name='Alice', phone_number='9800000001', balance=5000.0)
        bob = User(name='Bob', phone_number='9800000002', balance=1000.0)
        store_owner = User(name='Store Owner', phone_number='9800000003', balance=200.0, is_merchant=True)
        alice.set_password(bcrypt, 'pass1')
        bob.set_password(bcrypt, 'pass2')
        store_owner.set_password(bcrypt, 'pass3')

        db.session.add_all([alice, bob, store_owner])
        db.session.commit()

        cafe = Merchant(
            name='Kathmandu Coffee Co',
            emoji='☕',
            qr_code_id='QR_KTM_COFFEE',
            owner_user_id=store_owner.id
        )
        db.session.add(cafe)
        db.session.commit()

        print(f"Initialized Alice (NPR {alice.balance:.2f}), Bob (NPR {bob.balance:.2f}), Store Owner (NPR {store_owner.balance:.2f})")
        print(f"Initialized Merchant: {cafe.name} (Owner ID: {cafe.owner_user_id})")
        print("-" * 60)

        # -------------------------------------------------------------
        # 1. TEST transfer_funds: Successful transfer
        # -------------------------------------------------------------
        print("\n[TEST 1] Valid P2P Transfer (Alice -> Bob, NPR 1500.00)")
        tx1 = transfer_funds(alice.id, bob.phone_number, 1500.0)
        
        db.session.refresh(alice)
        db.session.refresh(bob)
        assert alice.balance == 3500.0, f"Expected Alice balance 3500, got {alice.balance}"
        assert bob.balance == 2500.0, f"Expected Bob balance 2500, got {bob.balance}"
        assert tx1.sender_id == alice.id
        assert tx1.receiver_id == bob.id
        assert tx1.amount == 1500.0
        assert tx1.type == 'sent'
        assert tx1.status == 'success'
        print(f"[OK] PASS: Alice balance = NPR {alice.balance:.2f}, Bob balance = NPR {bob.balance:.2f}, Tx ID #{tx1.id}")

        # -------------------------------------------------------------
        # 2. TEST transfer_funds: Insufficient balance
        # -------------------------------------------------------------
        print("\n[TEST 2] Insufficient Balance Transfer (Alice tries to send NPR 5000.00 with balance NPR 3500.00)")
        try:
            transfer_funds(alice.id, bob.phone_number, 5000.0)
            assert False, "Should have raised InsufficientBalanceError"
        except InsufficientBalanceError as e:
            db.session.refresh(alice)
            db.session.refresh(bob)
            assert alice.balance == 3500.0
            assert bob.balance == 2500.0
            print(f"[OK] PASS: Caught expected InsufficientBalanceError: '{e}'")
            print(f"[OK] Confirmed no balance change (Alice: {alice.balance:.2f}, Bob: {bob.balance:.2f})")

        # -------------------------------------------------------------
        # 3. TEST transfer_funds: Non-positive amount
        # -------------------------------------------------------------
        print("\n[TEST 3] Non-positive amounts (Zero and Negative)")
        for bad_amount in [0, -50.0]:
            try:
                transfer_funds(alice.id, bob.phone_number, bad_amount)
                assert False, f"Should have raised InvalidAmountError for amount {bad_amount}"
            except InvalidAmountError as e:
                print(f"[OK] PASS: Caught InvalidAmountError for amount {bad_amount}: '{e}'")

        # -------------------------------------------------------------
        # 4. TEST transfer_funds: Invalid recipient phone
        # -------------------------------------------------------------
        print("\n[TEST 4] Non-existent recipient phone")
        try:
            transfer_funds(alice.id, '9999999999', 100.0)
            assert False, "Should have raised InvalidRecipientError"
        except InvalidRecipientError as e:
            print(f"[OK] PASS: Caught InvalidRecipientError: '{e}'")

        # -------------------------------------------------------------
        # 5. TEST transfer_funds: Self-transfer
        # -------------------------------------------------------------
        print("\n[TEST 5] Self-transfer attempt (Alice -> Alice)")
        try:
            transfer_funds(alice.id, alice.phone_number, 100.0)
            assert False, "Should have raised InvalidRecipientError"
        except InvalidRecipientError as e:
            print(f"[OK] PASS: Caught InvalidRecipientError for self-transfer: '{e}'")

        # -------------------------------------------------------------
        # 6. TEST pay_merchant: Successful payment
        # -------------------------------------------------------------
        print("\n[TEST 6] Valid Merchant Payment (Alice pays Kathmandu Coffee Co, NPR 450.00)")
        tx2 = pay_merchant(alice.id, cafe.id, 450.0)
        
        db.session.refresh(alice)
        db.session.refresh(store_owner)
        assert alice.balance == 3050.0, f"Expected Alice balance 3050, got {alice.balance}"
        assert store_owner.balance == 650.0, f"Expected Store Owner balance 650, got {store_owner.balance}"
        assert tx2.sender_id == alice.id
        assert tx2.receiver_id is None, "Expected receiver_id to be None for merchant payment"
        assert tx2.merchant_name == 'Kathmandu Coffee Co'
        assert tx2.amount == 450.0
        assert tx2.type == 'paid'
        print(f"[OK] PASS: Alice balance = NPR {alice.balance:.2f}, Merchant Owner balance = NPR {store_owner.balance:.2f}, Tx ID #{tx2.id}")

        # -------------------------------------------------------------
        # 7. TEST pay_merchant: Insufficient balance
        # -------------------------------------------------------------
        print("\n[TEST 7] Merchant Payment with Insufficient Balance")
        try:
            pay_merchant(alice.id, cafe.id, 99999.0)
            assert False, "Should have raised InsufficientBalanceError"
        except InsufficientBalanceError as e:
            db.session.refresh(alice)
            assert alice.balance == 3050.0
            print(f"[OK] PASS: Caught InsufficientBalanceError: '{e}'")

        # -------------------------------------------------------------
        # 8. TEST pay_merchant: Non-positive amount
        # -------------------------------------------------------------
        print("\n[TEST 8] Merchant Payment with Non-positive amount")
        try:
            pay_merchant(alice.id, cafe.id, 0.0)
            assert False, "Should have raised InvalidAmountError"
        except InvalidAmountError as e:
            print(f"[OK] PASS: Caught InvalidAmountError: '{e}'")

        # -------------------------------------------------------------
        # 9. TEST pay_merchant: Invalid merchant ID
        # -------------------------------------------------------------
        print("\n[TEST 9] Merchant Payment to non-existent merchant")
        try:
            pay_merchant(alice.id, 99999, 100.0)
            assert False, "Should have raised InvalidRecipientError"
        except InvalidRecipientError as e:
            print(f"[OK] PASS: Caught InvalidRecipientError: '{e}'")

        # -------------------------------------------------------------
        # 10. TEST pay_merchant: Owner paying their own merchant
        # -------------------------------------------------------------
        print("\n[TEST 10] Owner paying their own merchant")
        try:
            pay_merchant(store_owner.id, cafe.id, 50.0)
            assert False, "Should have raised InvalidRecipientError"
        except InvalidRecipientError as e:
            print(f"[OK] PASS: Caught InvalidRecipientError: '{e}'")

        # -------------------------------------------------------------
        # 11. TEST Atomic Rollback on DB Error
        # -------------------------------------------------------------
        print("\n[TEST 11] Atomic rollback verification on simulated failure")
        initial_alice_balance = alice.balance
        initial_bob_balance = bob.balance

        from unittest.mock import patch
        with patch('models.db.session.commit', side_effect=RuntimeError("Simulated DB Disk/Connection Error")):
            try:
                transfer_funds(alice.id, bob.phone_number, 500.0)
                assert False, "Should have raised simulated RuntimeError"
            except RuntimeError as e:
                print(f"[OK] PASS: Simulated DB error caught: '{e}'")

        db.session.refresh(alice)
        db.session.refresh(bob)
        assert alice.balance == initial_alice_balance, f"Alice balance altered despite error: {alice.balance}"
        assert bob.balance == initial_bob_balance, f"Bob balance altered despite error: {bob.balance}"
        print(f"[OK] PASS: Balances were cleanly rolled back! Alice: NPR {alice.balance:.2f}, Bob: NPR {bob.balance:.2f}")

        print("\n" + "=" * 60)
        print("ALL 11 MANUAL TESTS PASSED SUCCESSFULLY!")
        print("=" * 60)

if __name__ == '__main__':
    run_tests()
