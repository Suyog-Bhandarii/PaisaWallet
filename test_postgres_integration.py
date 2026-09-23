"""PostgreSQL integration/concurrency tests.

Run with TEST_POSTGRES_URL=postgresql://... pytest -q test_postgres_integration.py.
The suite is skipped when PostgreSQL is not configured locally.
"""
import os
import threading
import unittest

from app import create_app
from config import TestConfig
from models import Transaction, User, db

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL is not configured")
class TestPostgresFinancialConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class PostgresConfig(TestConfig):
            SQLALCHEMY_DATABASE_URI = POSTGRES_URL
            SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

        cls.config = PostgresConfig
        cls.app = create_app(PostgresConfig)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            cls.sender = User(name="PG Sender", phone_number="9800000201", balance=10000)
            cls.receiver = User(name="PG Receiver", phone_number="9800000202", balance=0)
            db.session.add_all([cls.sender, cls.receiver])
            db.session.commit()
            cls.sender_id = cls.sender.id
            cls.receiver_phone = cls.receiver.phone_number

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.drop_all()
            db.session.remove()

    def test_concurrent_transfers_preserve_total_balance(self):
        errors = []

        def worker():
            try:
                local_app = create_app(self.config)
                with local_app.app_context():
                    from wallet_service import transfer_funds
                    transfer_funds(self.sender_id, self.receiver_phone, 100, is_paisa=False)
                    db.session.remove()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with self.app.app_context():
            sender = db.session.get(User, self.sender_id)
            receiver = User.query.filter_by(phone_number=self.receiver_phone).one()
            self.assertFalse(errors, errors)
            self.assertEqual(sender.balance_paisa, 900_000)
            self.assertEqual(receiver.balance_paisa, 100_000)
            self.assertEqual(Transaction.query.filter_by(type="sent").count(), 10)
