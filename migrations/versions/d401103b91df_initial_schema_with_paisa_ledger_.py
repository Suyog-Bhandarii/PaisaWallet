"""initial_schema_with_paisa_ledger_idempotency

Revision ID: d401103b91df
Revises: 
Create Date: 2026-09-22 23:33:47.087413

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd401103b91df'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # 1. users
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('phone_number', sa.String(length=20), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('balance_paisa', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='NPR'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('is_merchant', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('failed_login_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('balance_paisa >= 0', name='ck_user_balance_positive'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_phone_number'), ['phone_number'], unique=True)
        batch_op.create_index(batch_op.f('ix_users_status'), ['status'], unique=False)

    # 2. merchants
    op.create_table(
        'merchants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('emoji', sa.String(length=10), nullable=True),
        sa.Column('qr_code_id', sa.String(length=100), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name='fk_merchants_owner_user_id_users'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('merchants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_merchants_owner_user_id'), ['owner_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_merchants_qr_code_id'), ['qr_code_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_merchants_status'), ['status'], unique=False)

    # 3. transactions
    op.create_table(
        'transactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(length=40), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=True),
        sa.Column('receiver_id', sa.Integer(), nullable=True),
        sa.Column('merchant_id', sa.Integer(), nullable=True),
        sa.Column('merchant_name', sa.String(length=100), nullable=True),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('amount_paisa', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='NPR'),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='completed'),
        sa.CheckConstraint('amount_paisa > 0', name='ck_transaction_amount_positive'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_transactions_merchant_id_merchants'),
        sa.ForeignKeyConstraint(['receiver_id'], ['users.id'], name='fk_transactions_receiver_id_users'),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], name='fk_transactions_sender_id_users'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transactions_merchant_id'), ['merchant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_receiver_id'), ['receiver_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_reference'), ['reference'], unique=True)
        batch_op.create_index(batch_op.f('ix_transactions_sender_id'), ['sender_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_timestamp'), ['timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_type'), ['type'], unique=False)
        batch_op.create_index('ix_txn_merchant_ts', ['merchant_id', 'timestamp'], unique=False)
        batch_op.create_index('ix_txn_receiver_ts', ['receiver_id', 'timestamp'], unique=False)
        batch_op.create_index('ix_txn_sender_ts', ['sender_id', 'timestamp'], unique=False)

    # 4. ledger_entries
    op.create_table(
        'ledger_entries',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer, 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('amount_paisa', sa.BigInteger(), nullable=False),
        sa.Column('entry_type', sa.String(length=10), nullable=False),
        sa.Column('balance_after_paisa', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("entry_type IN ('debit', 'credit')", name='ck_ledger_entry_type'),
        sa.CheckConstraint('amount_paisa > 0', name='ck_ledger_amount_positive'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], name='fk_ledger_transaction_id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_ledger_user_id'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('ledger_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ledger_entries_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ledger_entries_transaction_id'), ['transaction_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ledger_entries_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_ledger_user_ts', ['user_id', 'created_at'], unique=False)

    # 5. idempotency_records
    op.create_table(
        'idempotency_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('operation', sa.String(length=50), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('response_code', sa.Integer(), nullable=False),
        sa.Column('response_body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_idempotency_user_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'key', 'operation', name='uq_idempotency_user_key_op')
    )
    with op.batch_alter_table('idempotency_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_idempotency_records_key'), ['key'], unique=False)
        batch_op.create_index(batch_op.f('ix_idempotency_records_user_id'), ['user_id'], unique=False)

    # 6. audit_logs
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_logs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_user_id'), ['user_id'], unique=False)


def downgrade():
    op.drop_table('audit_logs')
    op.drop_table('idempotency_records')
    op.drop_table('ledger_entries')
    op.drop_table('transactions')
    op.drop_table('merchants')
    op.drop_table('users')
