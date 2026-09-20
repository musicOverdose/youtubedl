"""telegram migrations table

Revision ID: 003_telegram_migrations_table
Revises: 002_telegram_settings_and_status
Create Date: 2026-09-20 16:20:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '003_telegram_migrations_table'
down_revision = '002_telegram_settings_and_status'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'telegram_migrations',
        sa.Column('id', sa.Integer(), primary_key=True, server_default=sa.text('1')),
        sa.Column('state', sa.String(length=64), nullable=False, server_default='IDLE'),
        sa.Column('logout_attempted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('id = 1', name='telegram_migrations_singleton'),
    )
    # Seed singleton row
    op.execute(
        "INSERT INTO telegram_migrations (id, state, created_at, updated_at) "
        "VALUES (1, 'IDLE', NOW(), NOW()) ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table('telegram_migrations')
