"""telegram settings and status

Revision ID: 002_telegram_settings_and_status
Revises: 001_initial_schema
Create Date: 2026-09-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '002_telegram_settings_and_status'
down_revision = '001_initial_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('settings', sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'))
    op.add_column('settings', sa.Column('is_encrypted', sa.Boolean(), nullable=False, server_default='false'))
    try:
        op.drop_constraint('settings_pkey', 'settings', type_='primary')
    except Exception:
        pass
    op.create_primary_key('pk_settings', 'settings', ['key', 'status'])


def downgrade() -> None:
    try:
        op.drop_constraint('pk_settings', 'settings', type_='primary')
    except Exception:
        pass
    op.create_primary_key('settings_pkey', 'settings', ['key'])
    op.drop_column('settings', 'is_encrypted')
    op.drop_column('settings', 'status')
