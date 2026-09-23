"""
Explicit development and demo data seed script.
WARNING: FOR LOCAL DEVELOPMENT / DEMO USE ONLY. NEVER RUN IN PRODUCTION.
"""
from app import bcrypt, create_app
from models import LedgerEntry, Merchant, Transaction, User, db
from money import DEFAULT_CURRENCY


def seed_development_data():
    """Seeds sample merchants and users with clearly labeled DEMO credentials."""
    print("Seeding development demo data...")

    # Check if demo merchant partner already exists
    demo_partner = User.query.filter_by(phone_number="9800000000").first()
    if not demo_partner:
        demo_partner = User(
            name="[DEMO] Paisa Merchant Partner",
            phone_number="9800000000",
            balance_paisa=1_000_000_00,  # NPR 1,000,000 in paisa
            is_merchant=True,
            currency=DEFAULT_CURRENCY,
            status="ACTIVE"
        )
        demo_partner.set_password(bcrypt, "DemoMerchantPass123!")
        db.session.add(demo_partner)
        db.session.flush()

        # Add initial ledger entry for starting balance
        init_tx = Transaction(
            reference=Transaction.generate_reference(),
            sender_id=None,
            receiver_id=demo_partner.id,
            merchant_id=None,
            type="topup",
            amount_paisa=demo_partner.balance_paisa,
            currency=DEFAULT_CURRENCY,
            status="completed"
        )
        db.session.add(init_tx)
        db.session.flush()

        db.session.add(LedgerEntry(
            transaction_id=init_tx.id,
            user_id=demo_partner.id,
            amount_paisa=demo_partner.balance_paisa,
            entry_type="credit",
            balance_after_paisa=demo_partner.balance_paisa
        ))
        db.session.commit()
        print("  - Created demo partner: 9800000000 / DemoMerchantPass123!")

    # Seed demo merchants
    if Merchant.query.first() is None:
        m1 = Merchant(
            name="Himalayan Java Cafe",
            emoji="☕",
            qr_code_id="qr_demo_hjc_001",
            owner_user_id=demo_partner.id,
            status="ACTIVE"
        )
        m2 = Merchant(
            name="Bhatbhateni Supermarket",
            emoji="🛒",
            qr_code_id="qr_demo_bbs_002",
            owner_user_id=demo_partner.id,
            status="ACTIVE"
        )
        db.session.add_all([m1, m2])
        db.session.commit()
        print("  - Seeded demo merchants: Himalayan Java Cafe & Bhatbhateni Supermarket")

    print("Demo data seeded successfully.")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        seed_development_data()
