"""harden financial invariants, roles and transactional idempotency

Revision ID: 7a3c8d1f4e22
Revises: d401103b91df
"""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = "7a3c8d1f4e22"
down_revision = "d401103b91df"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=20), nullable=False, server_default="USER"))
        batch_op.create_index("ix_users_role", ["role"], unique=False)
        batch_op.create_check_constraint("ck_user_role", "role IN ('USER','ADMIN')")
        batch_op.create_check_constraint("ck_user_status", "status IN ('ACTIVE','SUSPENDED','CLOSED')")

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("reversed_transaction_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_transactions_reversed_transaction_id", ["reversed_transaction_id"], unique=True)
        batch_op.create_foreign_key("fk_transactions_reversed_transaction_id", "transactions", ["reversed_transaction_id"], ["id"])
        batch_op.create_check_constraint("ck_transaction_type", "type IN ('sent','paid','topup','reversal')")
        batch_op.create_check_constraint("ck_transaction_status", "status IN ('pending','completed','failed','reversed')")

    with op.batch_alter_table("ledger_entries") as batch_op:
        batch_op.create_check_constraint("ck_ledger_balance_after_nonnegative", "balance_after_paisa >= 0")

    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.add_column(sa.Column("state", sa.String(length=20), nullable=False, server_default="completed"))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch_op.alter_column("response_code", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("response_body", existing_type=sa.Text(), nullable=True)
        batch_op.create_check_constraint("ck_idempotency_state", "state IN ('processing','completed','failed')")

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    op.get_bind().execute(sa.text("UPDATE idempotency_records SET expires_at = :expires_at"), {"expires_at": expires_at})
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.alter_column("expires_at", existing_type=sa.DateTime(), nullable=False)


def downgrade():
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.drop_constraint("ck_idempotency_state", type_="check")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("state")
        batch_op.alter_column("response_code", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("response_body", existing_type=sa.Text(), nullable=False)

    with op.batch_alter_table("ledger_entries") as batch_op:
        batch_op.drop_constraint("ck_ledger_balance_after_nonnegative", type_="check")

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("ck_transaction_status", type_="check")
        batch_op.drop_constraint("ck_transaction_type", type_="check")
        batch_op.drop_constraint("fk_transactions_reversed_transaction_id", type_="foreignkey")
        batch_op.drop_index("ix_transactions_reversed_transaction_id")
        batch_op.drop_column("reversed_transaction_id")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_user_status", type_="check")
        batch_op.drop_constraint("ck_user_role", type_="check")
        batch_op.drop_index("ix_users_role")
        batch_op.drop_column("role")
