"""
Comprehensive test suite for Paisa Wallet routes, APIs, authentication, and models.

Includes tests for:
- Financial math & strict precision
- Double-entry ledger integrity
- Idempotency replay & conflict detection
- Merchant dashboard isolation
- Velocity limits (single & daily caps)
- Account status lifecycle & lockout
- Nepal mobile number normalization
- Readiness probe & security headers
- SQLite concurrency regression tests
"""

import gc
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from app import bcrypt, create_app
from config import TestConfig
from models import (
    IdempotencyRecord,
    LedgerEntry,
    Merchant,
    Transaction,
    User,
    db,
)
from money import InvalidAmountError, npr_to_paisa, paisa_to_npr
from phone_utils import InvalidPhoneError, normalize_nepal_phone
from reconciliation import (
    reconcile_user_balance,
    run_full_reconciliation,
    validate_ledger_invariants,
)


class TestPaisaWallet(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    # --- Basic Route & Health Tests ---

    def test_public_wallet_homepage(self):
        """Test the public wallet landing page."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A simple wallet for everyday money.", response.data)
        self.assertIn(b"Create account", response.data)
        self.assertIn(b"Transfer money to another wallet.", response.data)
        self.assertNotIn(b"24,850.00", response.data)
        self.assertNotIn(b"Flask 3.x", response.data)
        self.assertNotIn(b"Probe API", response.data)
        self.assertIn(b"Paisa Wallet", response.data)

    def test_api_health_route(self):
        """Test the /api/health JSON route."""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["app"], "paisa-wallet")

    def test_api_ready_probe(self):
        """Test the /api/ready JSON readiness probe."""
        response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["database"], "ready")

    def test_security_headers_present(self):
        """Test that strict security headers are injected on responses."""
        res = self.client.get("/")

        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            res.headers.get("X-Content-Type-Options"),
            "nosniff",
        )
        self.assertEqual(
            res.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )
        self.assertIn(
            "default-src 'self'",
            res.headers.get("Content-Security-Policy", ""),
        )

    # --- HTML Authentication Flow Tests ---

    def test_signup_flow(self):
        """Test signup, password hashing, welcome balance, and auto-login."""
        get_res = self.client.get("/signup")
        self.assertEqual(get_res.status_code, 200)

        post_res = self.client.post(
            "/signup",
            data={
                "name": "Ram Bahadur",
                "phone_number": "9841234567",
                "password": "SecurePassword99",
                "confirm_password": "SecurePassword99",
            },
            follow_redirects=True,
        )

        self.assertEqual(post_res.status_code, 200)
        self.assertIn(b"Ram Bahadur", post_res.data)
        self.assertIn(b"NPR 5,000 welcome balance", post_res.data)

        user = User.query.filter_by(phone_number="9841234567").first()

        self.assertIsNotNone(user)
        self.assertEqual(user.balance, 5000.0)
        self.assertEqual(user.balance_paisa, 500000)
        self.assertTrue(
            user.check_password(bcrypt, "SecurePassword99")
        )

    def test_login_and_logout_flow(self):
        """Test login verification and session logout."""
        user = User(
            name="Sita Devi",
            phone_number="9849999999",
            balance=5000.0,
        )
        user.set_password(bcrypt, "SitaPass123!")

        db.session.add(user)
        db.session.commit()

        login_res = self.client.post(
            "/login",
            data={
                "phone_number": "9849999999",
                "password": "SitaPass123!",
            },
            follow_redirects=True,
        )

        self.assertEqual(login_res.status_code, 200)
        self.assertIn(b"Sita Devi", login_res.data)

        csrf_token = self.client.get(
            "/api/csrf-token"
        ).get_json()["csrf_token"]

        logout_res = self.client.post(
            "/logout",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )

        self.assertEqual(logout_res.status_code, 200)
        self.assertIn(b"logged out safely", logout_res.data)

    # --- JSON API: Unauthenticated Access ---

    def test_api_unauthorized_access(self):
        """Verify API routes return 401 JSON when unauthenticated."""
        for endpoint in [
            "/api/wallet",
            "/api/transactions",
            "/api/merchant/dashboard",
        ]:
            res = self.client.get(endpoint)

            self.assertEqual(res.status_code, 401)

            data = res.get_json()
            self.assertEqual(data["code"], "unauthorized")

        for endpoint, payload in [
            ("/api/send", {"phone": "9800000000", "amount": 100}),
            ("/api/pay", {"merchant_id": 1, "amount": 100}),
            ("/api/topup", {"amount": 100}),
        ]:
            res = self.client.post(endpoint, json=payload)

            self.assertEqual(res.status_code, 401)

            data = res.get_json()
            self.assertEqual(data["code"], "unauthorized")

    # --- JSON API: Authenticated Tests ---

    def _login_test_user(
        self,
        name="Tester",
        phone="9811111111",
        balance=5000.0,
        is_merchant=False,
    ):
        """Helper to create and log in a user."""
        user = User(
            name=name,
            phone_number=phone,
            balance=balance,
            is_merchant=is_merchant,
        )
        user.set_password(bcrypt, "password123")

        db.session.add(user)
        db.session.commit()

        self.client.post(
            "/login",
            data={
                "phone_number": phone,
                "password": "password123",
            },
        )

        return user

    def test_api_wallet_get(self):
        """Test GET /api/wallet returns current user balance and profile."""
        self._login_test_user(
            name="Aayush Sharma",
            phone="9840001122",
            balance=4250.75,
        )

        res = self.client.get("/api/wallet")

        self.assertEqual(res.status_code, 200)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["user"]["name"], "Aayush Sharma")
        self.assertEqual(
            data["user"]["phone_number"],
            "9840001122",
        )
        self.assertEqual(data["user"]["balance"], 4250.75)
        self.assertEqual(data["user"]["balance_paisa"], 425075)

    def test_api_send_success(self):
        """Test POST /api/send for valid fund transfer."""
        self._login_test_user(
            name="Alice",
            phone="9800000001",
            balance=5000.0,
        )

        bob = User(
            name="Bob",
            phone_number="9800000002",
            balance=1000.0,
        )
        bob.set_password(bcrypt, "pass")

        db.session.add(bob)
        db.session.commit()

        res = self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": 1200.0,
            },
        )

        self.assertEqual(res.status_code, 200)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["balance"], 3800.0)
        self.assertEqual(data["transaction"]["type"], "sent")
        self.assertEqual(data["transaction"]["amount"], 1200.0)
        self.assertEqual(
            data["transaction"]["amount_paisa"],
            120000,
        )
        self.assertTrue(
            data["transaction"]["reference"].startswith("TXN-")
        )

        db.session.refresh(bob)

        self.assertEqual(bob.balance, 2200.0)
        self.assertEqual(bob.balance_paisa, 220000)

    def test_api_send_failures(self):
        """Test POST /api/send error cases."""
        self._login_test_user(
            name="Alice",
            phone="9800000001",
            balance=500.0,
        )

        bob = User(
            name="Bob",
            phone_number="9800000002",
            balance=1000.0,
        )
        bob.set_password(bcrypt, "pass")

        db.session.add(bob)
        db.session.commit()

        res1 = self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": 1000.0,
            },
        )

        self.assertEqual(res1.status_code, 400)
        self.assertEqual(
            res1.get_json()["code"],
            "insufficient_balance",
        )

        res2 = self.client.post(
            "/api/send",
            json={
                "phone": "9800000001",
                "amount": 100.0,
            },
        )

        self.assertEqual(res2.status_code, 400)
        self.assertEqual(
            res2.get_json()["code"],
            "invalid_recipient",
        )

        res3 = self.client.post(
            "/api/send",
            json={
                "phone": "9999999999",
                "amount": 100.0,
            },
        )

        self.assertEqual(res3.status_code, 400)
        self.assertEqual(
            res3.get_json()["code"],
            "invalid_recipient",
        )

        res4 = self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": -50.0,
            },
        )

        self.assertEqual(res4.status_code, 400)
        self.assertEqual(
            res4.get_json()["code"],
            "invalid_amount",
        )

        res5 = self.client.post(
            "/api/send",
            json={},
        )

        self.assertEqual(res5.status_code, 400)
        self.assertEqual(
            res5.get_json()["code"],
            "missing_field",
        )

        res6 = self.client.post(
            "/api/send",
            data="not json",
            content_type="application/json",
        )

        self.assertEqual(res6.status_code, 400)
        self.assertEqual(
            res6.get_json()["code"],
            "invalid_payload",
        )

    def test_api_pay_merchant_success_and_failures(self):
        """Test POST /api/pay for merchant payments."""
        self._login_test_user(
            name="Alice",
            phone="9800000001",
            balance=2000.0,
        )

        owner = User(
            name="Owner",
            phone_number="9800000003",
            balance=0.0,
            is_merchant=True,
        )
        owner.set_password(bcrypt, "ownerpass")

        db.session.add(owner)
        db.session.commit()

        merchant = Merchant(
            name="Bakers Cafe",
            emoji="🥐",
            qr_code_id="QR_BAKERS",
            owner_user_id=owner.id,
            status="ACTIVE",
        )

        db.session.add(merchant)
        db.session.commit()

        res = self.client.post(
            "/api/pay",
            json={
                "merchant_id": merchant.id,
                "amount": 350.0,
            },
        )

        self.assertEqual(res.status_code, 200)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["balance"], 1650.0)
        self.assertEqual(data["transaction"]["type"], "paid")
        self.assertEqual(
            data["transaction"]["merchant_id"],
            merchant.id,
        )

        db.session.refresh(owner)

        self.assertEqual(owner.balance, 350.0)

        res_fail = self.client.post(
            "/api/pay",
            json={
                "merchant_id": merchant.id,
                "amount": 9999.0,
            },
        )

        self.assertEqual(res_fail.status_code, 400)
        self.assertEqual(
            res_fail.get_json()["code"],
            "insufficient_balance",
        )

        res_no_m = self.client.post(
            "/api/pay",
            json={
                "merchant_id": 9999,
                "amount": 100.0,
            },
        )

        self.assertEqual(res_no_m.status_code, 400)
        self.assertEqual(
            res_no_m.get_json()["code"],
            "invalid_recipient",
        )

    def test_api_topup_success_and_failures(self):
        """Test POST /api/topup crediting the user directly."""
        self._login_test_user(
            name="Topup User",
            phone="9800000099",
            balance=500.0,
        )

        res = self.client.post(
            "/api/topup",
            json={"amount": 1500.0},
        )

        self.assertEqual(res.status_code, 200)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["balance"], 2000.0)
        self.assertEqual(data["transaction"]["type"], "topup")
        self.assertEqual(data["transaction"]["amount"], 1500.0)

        res_bad = self.client.post(
            "/api/topup",
            json={"amount": 0.0},
        )

        self.assertEqual(res_bad.status_code, 400)
        self.assertEqual(
            res_bad.get_json()["code"],
            "invalid_amount",
        )

    def test_api_transactions_history_and_filters(self):
        """Test GET /api/transactions with filtering and pagination."""
        self._login_test_user(
            name="Alice",
            phone="9800000001",
            balance=5000.0,
        )

        bob = User(
            name="Bob",
            phone_number="9800000002",
            balance=5000.0,
        )
        bob.set_password(bcrypt, "pass")

        db.session.add(bob)
        db.session.commit()

        self.client.post(
            "/api/topup",
            json={"amount": 1000.0},
        )
        self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": 200.0,
            },
        )
        self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": 300.0,
            },
        )

        all_res = self.client.get("/api/transactions")

        self.assertEqual(all_res.status_code, 200)

        all_data = all_res.get_json()

        self.assertEqual(all_data["count"], 3)
        self.assertIn("pagination", all_data)

        topup_res = self.client.get(
            "/api/transactions?type=topup"
        )

        self.assertEqual(topup_res.status_code, 200)

        topup_data = topup_res.get_json()

        self.assertEqual(topup_data["count"], 1)
        self.assertEqual(
            topup_data["transactions"][0]["type"],
            "topup",
        )

        sent_res = self.client.get(
            "/api/transactions?type=sent"
        )

        self.assertEqual(sent_res.status_code, 200)
        self.assertEqual(
            sent_res.get_json()["count"],
            2,
        )

        lim_res = self.client.get(
            "/api/transactions?per_page=1"
        )

        self.assertEqual(lim_res.status_code, 200)
        self.assertEqual(
            lim_res.get_json()["count"],
            1,
        )
        self.assertTrue(
            lim_res.get_json()["pagination"]["has_next"]
        )

        bad_type = self.client.get(
            "/api/transactions?type=invalid"
        )
        self.assertEqual(bad_type.status_code, 400)

        bad_lim = self.client.get(
            "/api/transactions?per_page=abc"
        )
        self.assertEqual(bad_lim.status_code, 400)

    def test_api_receipt_lookup(self):
        """Test transaction receipt lookup."""
        self._login_test_user(
            name="Alice",
            phone="9800000001",
            balance=5000.0,
        )

        bob = User(
            name="Bob",
            phone_number="9800000002",
            balance=5000.0,
        )
        bob.set_password(bcrypt, "pass")

        db.session.add(bob)
        db.session.commit()

        send_res = self.client.post(
            "/api/send",
            json={
                "phone": "9800000002",
                "amount": 500.0,
            },
        )

        ref = send_res.get_json()["transaction"]["reference"]

        receipt_res = self.client.get(
            f"/api/transactions/{ref}"
        )

        self.assertEqual(receipt_res.status_code, 200)

        data = receipt_res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["receipt"]["reference"], ref)
        self.assertEqual(data["receipt"]["amount"], 500.0)

    # --- Merchant QR & Payment Confirmation Tests ---

    def test_merchant_qr_code_generation(self):
        """Test merchant QR code endpoint."""
        owner = User(
            name="Merchant Guy",
            phone_number="9800000055",
            balance=0.0,
            is_merchant=True,
        )
        owner.set_password(bcrypt, "pass")

        db.session.add(owner)
        db.session.commit()

        merchant = Merchant(
            name="Patan Tea House",
            emoji="🍵",
            qr_code_id="QR_PATAN_TEA",
            owner_user_id=owner.id,
            status="ACTIVE",
        )

        db.session.add(merchant)
        db.session.commit()

        res = self.client.get(
            f"/merchant/{merchant.id}/qr"
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, "image/png")
        self.assertTrue(
            res.data.startswith(b"\x89PNG\r\n\x1a\n")
        )

        res_404 = self.client.get(
            "/merchant/99999/qr"
        )

        self.assertEqual(res_404.status_code, 404)

    def test_pay_merchant_page_and_confirmation(self):
        """Test GET and POST merchant payment page."""
        payer = self._login_test_user(
            name="Payer User",
            phone="9800000088",
            balance=2500.0,
        )

        owner = User(
            name="Tea Owner",
            phone_number="9800000077",
            balance=100.0,
            is_merchant=True,
        )
        owner.set_password(bcrypt, "pass")

        db.session.add(owner)
        db.session.commit()

        merchant = Merchant(
            name="Pokhara Bakery",
            emoji="🥐",
            qr_code_id="QR_POKHARA_BAKERY",
            owner_user_id=owner.id,
            status="ACTIVE",
        )

        db.session.add(merchant)
        db.session.commit()

        res_get = self.client.get(
            "/pay/QR_POKHARA_BAKERY"
        )

        self.assertEqual(res_get.status_code, 200)
        self.assertIn(b"Pokhara Bakery", res_get.data)
        self.assertIn(b"QR_POKHARA_BAKERY", res_get.data)

        res_bad_qr = self.client.get(
            "/pay/INVALID_QR_CODE"
        )

        self.assertEqual(res_bad_qr.status_code, 404)

        res_post = self.client.post(
            "/pay/QR_POKHARA_BAKERY",
            data={"amount": "450.00"},
            follow_redirects=True,
        )

        self.assertEqual(res_post.status_code, 200)
        self.assertIn(
            b"Payment of NPR 450.00 to Pokhara Bakery was successful!",
            res_post.data,
        )

        db.session.refresh(payer)
        db.session.refresh(owner)

        self.assertEqual(payer.balance, 2050.0)
        self.assertEqual(owner.balance, 550.0)

        res_insufficient = self.client.post(
            "/pay/QR_POKHARA_BAKERY",
            data={"amount": "5000.00"},
            follow_redirects=True,
        )

        self.assertEqual(res_insufficient.status_code, 200)
        self.assertIn(
            b"Insufficient balance",
            res_insufficient.data,
        )

    def test_merchant_registration_opt_in(self):
        """Test merchant registration opt-in."""
        user = self._login_test_user(
            name="Regular User",
            phone="9812345678",
            balance=5000.0,
            is_merchant=False,
        )

        self.assertFalse(user.is_merchant)

        res = self.client.post(
            "/merchant/register",
            json={
                "name": "Himalayan Organic Tea",
                "emoji": "🍃",
            },
        )

        self.assertEqual(res.status_code, 201)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(
            data["merchant"]["name"],
            "Himalayan Organic Tea",
        )
        self.assertTrue(
            data["merchant"]["qr_code_id"].startswith(
                "qr_sec_"
            )
        )

        db.session.refresh(user)

        self.assertTrue(user.is_merchant)

        merchant = Merchant.query.filter_by(
            name="Himalayan Organic Tea"
        ).first()

        self.assertIsNotNone(merchant)
        self.assertEqual(
            merchant.owner_user_id,
            user.id,
        )

    def test_api_merchant_dashboard_metrics_and_pagination(self):
        """Test merchant dashboard metrics and pagination."""
        merchant_user = self._login_test_user(
            name="Coffee Shop Owner",
            phone="9841122334",
            balance=0.0,
            is_merchant=True,
        )

        merchant = Merchant(
            name="Java Central",
            emoji="☕",
            qr_code_id="QR_JAVA_CENTRAL",
            owner_user_id=merchant_user.id,
            status="ACTIVE",
        )

        db.session.add(merchant)
        db.session.commit()

        customer = User(
            name="Coffee Drinker",
            phone_number="9877777777",
            balance=5000.0,
        )
        customer.set_password(bcrypt, "pass")

        db.session.add(customer)
        db.session.commit()

        tx1 = Transaction(
            sender_id=customer.id,
            receiver_id=merchant_user.id,
            merchant_id=merchant.id,
            merchant_name="Java Central",
            type="paid",
            amount=250.0,
        )

        tx2 = Transaction(
            sender_id=customer.id,
            receiver_id=merchant_user.id,
            merchant_id=merchant.id,
            merchant_name="Java Central",
            type="paid",
            amount=350.0,
        )

        tx3 = Transaction(
            sender_id=customer.id,
            receiver_id=merchant_user.id,
            merchant_id=merchant.id,
            merchant_name="Java Central",
            type="paid",
            amount=400.0,
        )

        db.session.add_all([tx1, tx2, tx3])
        db.session.commit()

        res = self.client.get(
            "/api/merchant/dashboard?page=1&per_page=2"
        )

        self.assertEqual(res.status_code, 200)

        data = res.get_json()

        self.assertEqual(data["status"], "success")
        self.assertTrue(data["is_merchant"])
        self.assertEqual(
            data["total_received_today"],
            1000.0,
        )
        self.assertEqual(
            data["count_payments_today"],
            3,
        )
        self.assertEqual(
            len(data["transactions"]),
            2,
        )
        self.assertTrue(
            data["pagination"]["has_next"]
        )


class TestMoneyAndPrecision(unittest.TestCase):
    """Test integer paisa calculations and strict Decimal validation."""

    def test_npr_to_paisa_conversions(self):
        self.assertEqual(npr_to_paisa("100"), 10000)
        self.assertEqual(npr_to_paisa(100), 10000)
        self.assertEqual(npr_to_paisa(100.5), 10050)
        self.assertEqual(npr_to_paisa("100.55"), 10055)
        self.assertEqual(
            npr_to_paisa(Decimal("100.55")),
            10055,
        )

    def test_paisa_to_npr_conversions(self):
        self.assertEqual(
            paisa_to_npr(10000),
            Decimal("100.00"),
        )
        self.assertEqual(
            paisa_to_npr(10055),
            Decimal("100.55"),
        )

    def test_rejection_of_invalid_monetary_inputs(self):
        for invalid in [
            0,
            -10,
            "-50",
            "NaN",
            "Infinity",
            "10.999",
            None,
            True,
            False,
        ]:
            with self.assertRaises(InvalidAmountError):
                npr_to_paisa(invalid)


class TestIdempotency(unittest.TestCase):
    """Verify idempotency replay and conflict detection."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

        self.user = User(
            name="Idem User",
            phone_number="9811112222",
            balance=5000.0,
        )
        self.user.set_password(bcrypt, "pass123")

        db.session.add(self.user)
        db.session.commit()

        self.client.post(
            "/login",
            data={
                "phone_number": "9811112222",
                "password": "pass123",
            },
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_topup_idempotency_prevents_duplicate_credits(self):
        idem_key = "test-idem-topup-001"
        payload = {"amount": 500.0}
        headers = {"Idempotency-Key": idem_key}

        res1 = self.client.post(
            "/api/topup",
            json=payload,
            headers=headers,
        )

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(
            res1.get_json()["balance"],
            5500.0,
        )

        res2 = self.client.post(
            "/api/topup",
            json=payload,
            headers=headers,
        )

        self.assertEqual(res2.status_code, 200)
        self.assertEqual(
            res2.get_json()["balance"],
            5500.0,
        )

        db.session.refresh(self.user)

        self.assertEqual(
            self.user.balance_paisa,
            550000,
        )

    def test_idempotency_conflict_returns_409(self):
        idem_key = "test-idem-conflict-001"
        headers = {"Idempotency-Key": idem_key}

        res1 = self.client.post(
            "/api/topup",
            json={"amount": 500.0},
            headers=headers,
        )

        self.assertEqual(res1.status_code, 200)

        res2 = self.client.post(
            "/api/topup",
            json={"amount": 1000.0},
            headers=headers,
        )

        self.assertEqual(res2.status_code, 409)
        self.assertEqual(
            res2.get_json()["code"],
            "idempotency_conflict",
        )


class TestMerchantIsolation(unittest.TestCase):
    """Verify merchant dashboard isolation."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_merchant_cannot_see_other_merchant_data(self):
        user_a = User(
            name="Merchant A",
            phone_number="9800000010",
            balance=0.0,
            is_merchant=True,
        )
        user_a.set_password(bcrypt, "passA")

        user_b = User(
            name="Merchant B",
            phone_number="9800000020",
            balance=0.0,
            is_merchant=True,
        )
        user_b.set_password(bcrypt, "passB")

        customer = User(
            name="Customer",
            phone_number="9800000030",
            balance=10000.0,
        )
        customer.set_password(bcrypt, "passC")

        db.session.add_all([
            user_a,
            user_b,
            customer,
        ])
        db.session.commit()

        m_a = Merchant(
            name="Store A",
            qr_code_id="QR_STORE_A",
            owner_user_id=user_a.id,
            status="ACTIVE",
        )

        m_b = Merchant(
            name="Store B",
            qr_code_id="QR_STORE_B",
            owner_user_id=user_b.id,
            status="ACTIVE",
        )

        db.session.add_all([m_a, m_b])
        db.session.commit()

        tx_b = Transaction(
            sender_id=customer.id,
            receiver_id=user_b.id,
            merchant_id=m_b.id,
            merchant_name="Store B",
            type="paid",
            amount=500.0,
        )

        db.session.add(tx_b)
        db.session.commit()

        self.client.post(
            "/login",
            data={
                "phone_number": "9800000010",
                "password": "passA",
            },
        )

        res_a = self.client.get(
            "/api/merchant/dashboard"
        )

        self.assertEqual(res_a.status_code, 200)

        data_a = res_a.get_json()

        self.assertEqual(
            data_a["total_received_today"],
            0.0,
        )
        self.assertEqual(
            data_a["count_payments_today"],
            0,
        )
        self.assertEqual(
            len(data_a["transactions"]),
            0,
        )


class TestVelocityLimits(unittest.TestCase):
    """Verify single-transaction and daily cumulative caps."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

        self.user = User(
            name="Rich User",
            phone_number="9800000088",
            balance=500000.0,
        )
        self.user.set_password(bcrypt, "pass")

        self.recipient = User(
            name="Receiver",
            phone_number="9800000099",
            balance=0.0,
        )
        self.recipient.set_password(bcrypt, "pass")

        db.session.add_all([
            self.user,
            self.recipient,
        ])
        db.session.commit()

        self.client.post(
            "/login",
            data={
                "phone_number": "9800000088",
                "password": "pass",
            },
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_single_transfer_limit_exceeded(self):
        res = self.client.post(
            "/api/send",
            json={
                "phone": "9800000099",
                "amount": 50001.0,
            },
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.get_json()["code"],
            "limit_exceeded",
        )

    def test_single_topup_limit_exceeded(self):
        res = self.client.post(
            "/api/topup",
            json={"amount": 25001.0},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.get_json()["code"],
            "limit_exceeded",
        )


class TestAccountLifecycleAndLockout(unittest.TestCase):
    """Verify account suspension and failed login lockout."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_suspended_account_cannot_transact(self):
        user = User(
            name="Suspended",
            phone_number="9800000077",
            balance=5000.0,
            status="SUSPENDED",
        )
        user.set_password(bcrypt, "pass")

        db.session.add(user)
        db.session.commit()

        login_res = self.client.post(
            "/login",
            json={
                "phone": "9800000077",
                "password": "pass",
            },
        )

        self.assertEqual(login_res.status_code, 403)
        self.assertEqual(
            login_res.get_json()["code"],
            "account_inactive",
        )

    def test_account_locks_after_5_failed_logins(self):
        user = User(
            name="Target",
            phone_number="9800000066",
            balance=1000.0,
        )
        user.set_password(
            bcrypt,
            "RealPassword1!",
        )

        db.session.add(user)
        db.session.commit()

        for _ in range(5):
            self.client.post(
                "/login",
                json={
                    "phone": "9800000066",
                    "password": "WrongPassword",
                },
            )

        db.session.refresh(user)

        self.assertIsNotNone(user.locked_until)

        res = self.client.post(
            "/login",
            json={
                "phone": "9800000066",
                "password": "RealPassword1!",
            },
        )

        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            res.get_json()["code"],
            "account_locked",
        )


class TestNepalPhoneNormalization(unittest.TestCase):
    """Verify standardizing Nepal mobile numbers."""

    def test_valid_phone_formats(self):
        self.assertEqual(
            normalize_nepal_phone("9841234567"),
            "9841234567",
        )
        self.assertEqual(
            normalize_nepal_phone("+977-9841234567"),
            "9841234567",
        )
        self.assertEqual(
            normalize_nepal_phone("977 9841234567"),
            "9841234567",
        )
        self.assertEqual(
            normalize_nepal_phone("09841234567"),
            "9841234567",
        )
        self.assertEqual(
            normalize_nepal_phone("9741234567"),
            "9741234567",
        )

    def test_invalid_phone_formats(self):
        for bad in [
            "12345",
            "9641234567",
            "984123456",
            "98412345678",
            "abcdefghij",
            "",
        ]:
            with self.assertRaises(InvalidPhoneError):
                normalize_nepal_phone(bad)


class TestCSRFEnforcement(unittest.TestCase):
    """Verify CSRF protection."""

    def setUp(self):
        class CSRFConfig(TestConfig):
            WTF_CSRF_ENABLED = True

        self.app = create_app(CSRFConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

        self.user = User(
            name="CSRF Tester",
            phone_number="9877777777",
            balance=5000.0,
        )
        self.user.set_password(bcrypt, "Pass123!")

        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_post_without_csrf_token_is_blocked(self):
        res = self.client.post(
            "/api/topup",
            json={"amount": 500},
        )

        self.assertEqual(res.status_code, 400)

        data = res.get_json()

        self.assertEqual(
            data["code"],
            "csrf_error",
        )

        error_msg = (
            data["error"]["message"]
            if isinstance(data["error"], dict)
            else data["error"]
        )

        self.assertIn(
            "CSRF token is missing",
            error_msg,
        )

    def test_post_with_valid_csrf_token_header_succeeds(self):
        csrf_res = self.client.get(
            "/api/csrf-token"
        )

        self.assertEqual(csrf_res.status_code, 200)

        token = csrf_res.get_json()["csrf_token"]

        login_res = self.client.post(
            "/login",
            data={
                "phone_number": "9877777777",
                "password": "Pass123!",
                "csrf_token": token,
            },
            follow_redirects=True,
        )

        self.assertEqual(login_res.status_code, 200)

        topup_res = self.client.post(
            "/api/topup",
            json={"amount": 750},
            headers={"X-CSRFToken": token},
        )

        self.assertEqual(topup_res.status_code, 200)

        topup_data = topup_res.get_json()

        self.assertEqual(
            topup_data["status"],
            "success",
        )
        self.assertEqual(
            topup_data["balance"],
            5750.0,
        )


class TestLoginRateLimiting(unittest.TestCase):
    """Verify /login rate limiting."""

    def setUp(self):
        class RateLimitConfig(TestConfig):
            RATELIMIT_ENABLED = True
            RATELIMIT_STORAGE_URI = "memory://"

        self.app = create_app(RateLimitConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

        user = User(
            name="Rate Limited User",
            phone_number="9812345678",
            balance=1000.0,
        )
        user.set_password(
            bcrypt,
            "CorrectPass",
        )

        db.session.add(user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_login_rate_limit_exceeded(self):
        for i in range(5):
            res = self.client.post(
                "/login",
                data={
                    "phone_number": "9812345678",
                    "password": f"WrongPass_{i}",
                },
            )

            self.assertEqual(
                res.status_code,
                200,
                f"Attempt {i + 1} should be processed",
            )

        res_blocked = self.client.post(
            "/login",
            data={
                "phone_number": "9812345678",
                "password": "WrongPass_6",
            },
        )

        self.assertEqual(
            res_blocked.status_code,
            429,
        )


class TestReconciliation(unittest.TestCase):
    """Verify balance reconciliation."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_reconciliation_audit(self):
        self.client.post(
            "/signup",
            data={
                "name": "Recon User",
                "phone_number": "9841999999",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        user = User.query.filter_by(
            phone_number="9841999999"
        ).first()

        self.assertIsNotNone(user)

        report = reconcile_user_balance(user.id)

        self.assertEqual(
            report["status"],
            "MATCHED",
        )
        self.assertEqual(
            report["discrepancy_paisa"],
            0,
        )

        reports = run_full_reconciliation()

        self.assertTrue(len(reports) >= 1)
        self.assertTrue(
            all(
                r["status"] == "MATCHED"
                for r in reports
            )
        )


class TestFinancialCorrectnessHardening(unittest.TestCase):
    """Regression tests for atomicity, ledger invariants and reversals."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        db.create_all()

        self.admin = User(
            name="Admin",
            phone_number="9800000100",
            role="ADMIN",
            balance=0,
        )

        self.sender = User(
            name="Sender",
            phone_number="9800000101",
            balance=5000,
        )

        self.receiver = User(
            name="Receiver",
            phone_number="9800000102",
            balance=1000,
        )

        for user in (
            self.admin,
            self.sender,
            self.receiver,
        ):
            user.set_password(
                bcrypt,
                "Password123!",
            )

        db.session.add_all([
            self.admin,
            self.sender,
            self.receiver,
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_ledger_invariant_and_immutability(self):
        from wallet_service import transfer_funds

        tx = transfer_funds(
            self.sender.id,
            self.receiver.phone_number,
            500,
            is_paisa=False,
        )

        report = validate_ledger_invariants(tx.id)

        self.assertEqual(
            report["status"],
            "VALID",
        )

        entries = LedgerEntry.query.filter_by(
            transaction_id=tx.id
        ).all()

        self.assertEqual(len(entries), 2)

        entries[0].amount_paisa += 1

        with self.assertRaises(ValueError):
            db.session.flush()

        db.session.rollback()

        entry = LedgerEntry.query.filter_by(
            transaction_id=tx.id
        ).first()

        db.session.delete(entry)

        with self.assertRaises(ValueError):
            db.session.flush()

        db.session.rollback()

    def test_reversal_is_admin_only_and_idempotent(self):
        from wallet_service import (
            AdminAuthorizationError,
            reverse_transaction,
            transfer_funds,
        )

        tx = transfer_funds(
            self.sender.id,
            self.receiver.phone_number,
            250,
            is_paisa=False,
        )

        with self.assertRaises(AdminAuthorizationError):
            reverse_transaction(
                tx.id,
                actor_user_id=self.sender.id,
            )

        reversal = reverse_transaction(
            tx.id,
            actor_user_id=self.admin.id,
            reason="Test correction",
        )

        self.assertEqual(
            reversal.type,
            "reversal",
        )
        self.assertEqual(
            tx.status,
            "reversed",
        )

        same = reverse_transaction(
            tx.id,
            actor_user_id=self.admin.id,
            reason="Replay",
        )

        self.assertEqual(
            same.id,
            reversal.id,
        )

        self.assertEqual(
            Transaction.query.filter_by(
                type="reversal"
            ).count(),
            1,
        )

    def test_failure_rolls_back_balances_and_ledger(self):
        from wallet_service import transfer_funds

        before_sender = self.sender.balance_paisa
        before_receiver = self.receiver.balance_paisa

        with patch.object(
            db.session,
            "commit",
            side_effect=RuntimeError(
                "injected commit failure"
            ),
        ):
            with self.assertRaises(RuntimeError):
                transfer_funds(
                    self.sender.id,
                    self.receiver.phone_number,
                    100,
                    is_paisa=False,
                )

        db.session.expire_all()

        self.assertEqual(
            db.session.get(
                User,
                self.sender.id,
            ).balance_paisa,
            before_sender,
        )

        self.assertEqual(
            db.session.get(
                User,
                self.receiver.id,
            ).balance_paisa,
            before_receiver,
        )

        self.assertEqual(
            Transaction.query.count(),
            0,
        )

        self.assertEqual(
            LedgerEntry.query.count(),
            0,
        )


class TestSecurityRegression(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_logout_is_post_only(self):
        self.assertEqual(
            self.client.get("/logout").status_code,
            405,
        )

    def test_csp_has_no_script_unsafe_inline(self):
        response = self.client.get("/")

        csp = response.headers[
            "Content-Security-Policy"
        ]

        self.assertNotIn(
            "script-src 'self' 'unsafe-inline'",
            csp,
        )

        self.assertIn(
            "script-src 'self' 'nonce-",
            csp,
        )

        self.assertNotIn(
            "fonts.googleapis.com",
            csp,
        )

        self.assertNotIn(
            "cdn.jsdelivr.net",
            csp,
        )

    def test_ready_does_not_leak_database_exception(self):
        with patch.object(
            db.session,
            "execute",
            side_effect=RuntimeError(
                "secret-db-password"
            ),
        ):
            response = self.client.get("/api/ready")

        self.assertEqual(
            response.status_code,
            503,
        )

        payload = response.get_json()

        self.assertNotIn(
            "secret-db-password",
            response.get_data(as_text=True),
        )

        self.assertEqual(
            payload["status"],
            "not_ready",
        )

        self.assertIn(
            "message",
            payload,
        )


class TestIdempotencyReservation(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        db.create_all()

        self.user = User(
            name="Idem",
            phone_number="9800000110",
            balance=5000,
        )

        self.recipient = User(
            name="Recipient",
            phone_number="9800000111",
            balance=0,
        )

        for user in (
            self.user,
            self.recipient,
        ):
            user.set_password(
                bcrypt,
                "Password123!",
            )

        db.session.add_all([
            self.user,
            self.recipient,
        ])
        db.session.commit()

        self.client.post(
            "/login",
            data={
                "phone_number": self.user.phone_number,
                "password": "Password123!",
            },
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_replay_returns_exact_cached_response(self):
        key = "atomic-key-1"

        payload = {
            "phone": self.recipient.phone_number,
            "amount": 100,
        }

        first = self.client.post(
            "/api/send",
            json=payload,
            headers={"Idempotency-Key": key},
        )

        second = self.client.post(
            "/api/send",
            json=payload,
            headers={"Idempotency-Key": key},
        )

        self.assertEqual(
            first.status_code,
            second.status_code,
        )

        self.assertEqual(
            first.get_json(),
            second.get_json(),
        )

        self.assertEqual(
            Transaction.query.filter_by(
                type="sent"
            ).count(),
            1,
        )

        self.assertEqual(
            IdempotencyRecord.query.filter_by(
                key=key
            ).count(),
            1,
        )


class TestConcurrencySQLite(unittest.TestCase):
    """
    SQLite concurrency regression tests.

    These tests verify that concurrent financial operations:
    - do not lose updates,
    - enforce daily limits atomically,
    - serialize concurrent top-ups,
    - leave the database in a consistent state.

    SQLite is used here as a local concurrency regression environment.
    PostgreSQL remains the authoritative environment for production-style
    row-lock/concurrency integration testing.
    """

    @staticmethod
    def _dispose_sqlite_app(app):
        """
        Properly release SQLAlchemy connections before deleting the SQLite DB.

        Windows keeps SQLite files locked while a connection is still alive,
        so simply calling os.remove() can produce WinError 32.
        """
        if app is None:
            return

        try:
            with app.app_context():
                try:
                    db.session.rollback()
                except Exception:
                    pass

                try:
                    db.session.remove()
                except Exception:
                    pass

                try:
                    db.engine.dispose()
                except Exception:
                    pass
        except Exception:
            pass

        gc.collect()

    @staticmethod
    def _remove_sqlite_database(path, retries=15, delay=0.2):
        """
        Remove SQLite database and WAL/SHM files safely on Windows.

        SQLite connections can briefly remain referenced after a threaded
        test finishes, so retry deletion instead of failing the test during
        cleanup.
        """
        gc.collect()

        suffixes = ("", "-wal", "-shm")

        for _ in range(retries):
            all_removed = True

            for suffix in suffixes:
                file_path = path + suffix

                if not os.path.exists(file_path):
                    continue

                try:
                    os.remove(file_path)
                except PermissionError:
                    all_removed = False

            if all_removed:
                return

            gc.collect()
            time.sleep(delay)

        # Do not hide a genuinely locked database forever.
        remaining = [
            path + suffix
            for suffix in suffixes
            if os.path.exists(path + suffix)
        ]

        if remaining:
            raise PermissionError(
                "SQLite database files are still locked after cleanup retries: "
                + ", ".join(remaining)
            )

    def test_concurrent_topups_are_serialized(self):
        """
        Ten concurrent top-ups of NPR 100 must result in exactly:

            balance = NPR 1,000
            transaction count = 10
            no worker errors

        The operation must not lose credits under concurrent execution.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        app = None
        worker_apps = []
        errors = []

        try:
            class FileTestConfig(TestConfig):
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"

                SQLALCHEMY_ENGINE_OPTIONS = {
                    "connect_args": {
                        "check_same_thread": False,
                        "timeout": 30,
                    }
                }

            app = create_app(FileTestConfig)

            with app.app_context():
                db.create_all()

                user = User(
                    name="Concurrent",
                    phone_number="9800000120",
                    balance=0,
                )

                # password_hash is NOT NULL in the model/database.
                user.set_password(
                    bcrypt,
                    "ConcurrentTestPassword123!",
                )

                db.session.add(user)
                db.session.commit()

                user_id = user.id

            successes = []

            def worker():
                local_app = None

                try:
                    local_app = create_app(FileTestConfig)
                    worker_apps.append(local_app)

                    with local_app.app_context():
                        from wallet_service import topup_wallet

                        tx = topup_wallet(
                            user_id,
                            100,
                            is_paisa=False,
                        )

                        successes.append(tx.reference)

                        db.session.remove()

                except Exception as exc:
                    errors.append(exc)

                finally:
                    if local_app is not None:
                        self._dispose_sqlite_app(local_app)

            threads = [
                threading.Thread(
                    target=worker,
                    name=f"topup-worker-{index}",
                )
                for index in range(10)
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            # Make sure all worker SQLAlchemy engines are disposed.
            for worker_app in worker_apps:
                self._dispose_sqlite_app(worker_app)

            with app.app_context():
                db.session.expire_all()

                user = db.session.get(User, user_id)

                transaction_count = Transaction.query.filter_by(
    type="topup",
).count()

                self.assertEqual(
                    errors,
                    [],
                    f"Concurrent top-up workers failed: {errors}",
                )

                self.assertEqual(
                    len(successes),
                    10,
                    "All 10 concurrent top-ups should succeed.",
                )

                self.assertEqual(
                    user.balance_paisa,
                    100_000,
                    "10 × NPR 100 must equal NPR 1,000 = 100,000 paisa.",
                )

                self.assertEqual(
                    transaction_count,
                    10,
                    "Exactly 10 top-up transactions should exist.",
                )

        finally:
            for worker_app in worker_apps:
                self._dispose_sqlite_app(worker_app)

            self._dispose_sqlite_app(app)

            self._remove_sqlite_database(path)

    def test_concurrent_daily_transfer_limit_is_atomic(self):
        """
        Ten concurrent transfers of NPR 50,000 are attempted against a
        daily transfer limit of NPR 200,000.

        Expected result:

            successful transfers = 4
            failed transfers = 6

            sender:
                NPR 500,000 - NPR 200,000
                = NPR 300,000
                = 30,000,000 paisa

            receiver:
                NPR 200,000
                = 20,000,000 paisa

        The important property is that the daily limit is checked atomically
        inside the financial transaction rather than by an unsafe
        read-then-write sequence.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        app = None
        worker_apps = []
        successes = []
        failures = []

        try:
            class FileTestConfig(TestConfig):
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"

                SQLALCHEMY_ENGINE_OPTIONS = {
                    "connect_args": {
                        "check_same_thread": False,
                        "timeout": 30,
                    }
                }

            app = create_app(FileTestConfig)

            with app.app_context():
                db.create_all()

                sender = User(
                    name="Daily Sender",
                    phone_number="9800000130",
                    balance=500000,
                )

                receiver = User(
                    name="Daily Receiver",
                    phone_number="9800000131",
                    balance=0,
                )

                # password_hash is NOT NULL in the model/database.
                sender.set_password(
                    bcrypt,
                    "ConcurrentTestPassword123!",
                )

                receiver.set_password(
                    bcrypt,
                    "ConcurrentTestPassword123!",
                )

                db.session.add_all([
                    sender,
                    receiver,
                ])

                db.session.commit()

                sender_id = sender.id
                receiver_phone = receiver.phone_number

            lock = threading.Lock()

            def worker():
                local_app = None

                try:
                    local_app = create_app(FileTestConfig)
                    worker_apps.append(local_app)

                    with local_app.app_context():
                        from wallet_service import transfer_funds

                        tx = transfer_funds(
                            sender_id,
                            receiver_phone,
                            50000,
                            is_paisa=False,
                        )

                        with lock:
                            successes.append(tx.reference)

                        db.session.remove()

                except Exception as exc:
                    with lock:
                        failures.append(exc)

                finally:
                    if local_app is not None:
                        self._dispose_sqlite_app(local_app)

            threads = [
                threading.Thread(
                    target=worker,
                    name=f"transfer-worker-{index}",
                )
                for index in range(10)
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            # Dispose all worker engines before reading/cleaning the DB.
            for worker_app in worker_apps:
                self._dispose_sqlite_app(worker_app)

            with app.app_context():
                db.session.expire_all()

                sender = db.session.get(
                    User,
                    sender_id,
                )

                receiver = User.query.filter_by(
                    phone_number=receiver_phone,
                ).one()

                self.assertEqual(
                    len(successes),
                    4,
                    "Exactly four NPR 50,000 transfers should fit "
                    "inside the NPR 200,000 daily limit.",
                )

                self.assertEqual(
                    len(failures),
                    6,
                    "The remaining six transfers must be rejected.",
                )

                self.assertEqual(
                    sender.balance_paisa,
                    30_000_000,
                    "Sender must retain NPR 300,000.",
                )

                self.assertEqual(
                    receiver.balance_paisa,
                    20_000_000,
                    "Receiver must receive NPR 200,000.",
                )

        finally:
            for worker_app in worker_apps:
                self._dispose_sqlite_app(worker_app)

            self._dispose_sqlite_app(app)

            self._remove_sqlite_database(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
