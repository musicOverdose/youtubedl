"""initial schema

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-09-19 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. users
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('first_name', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='USER'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('total_jobs', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('successful_jobs', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_jobs', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 2. jobs
    op.create_table(
        'jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('canonical_url', sa.Text(), nullable=False),
        sa.Column('source_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('operation', sa.String(length=32), nullable=False),
        sa.Column('output_codec', sa.String(length=32), nullable=True),
        sa.Column('resolution', sa.String(length=32), nullable=True),
        sa.Column('target_height', sa.Integer(), nullable=True),
        sa.Column('subtitle_lang', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('cache_key', sa.String(length=64), nullable=False),
        sa.Column('progress', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('speed', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('eta', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_jobs_source_id', 'jobs', ['source_id'])
    op.create_index('ix_jobs_status', 'jobs', ['status'])
    op.create_index('ix_jobs_cache_key', 'jobs', ['cache_key'])
    op.create_index('ix_jobs_status_created', 'jobs', ['status', 'created_at'])

    # 3. job_requests
    op.create_table(
        'job_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('status_message_id', sa.BigInteger(), nullable=True),
        sa.Column('delivery_status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_requests_job_id', 'job_requests', ['job_id'])
    op.create_index('ix_job_requests_user_id', 'job_requests', ['user_id'])

    # 4. cache_entries
    op.create_table(
        'cache_entries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cache_key', sa.String(length=64), nullable=False),
        sa.Column('source_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('operation', sa.String(length=32), nullable=False),
        sa.Column('codec', sa.String(length=32), nullable=True),
        sa.Column('resolution', sa.String(length=32), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('subtitle_lang', sa.String(length=16), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('telegram_channel_id', sa.BigInteger(), nullable=False),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_valid', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cache_entries_cache_key', 'cache_entries', ['cache_key'], unique=True)
    op.create_index('ix_cache_entries_source_id', 'cache_entries', ['source_id'])

    # 5. required_channels
    op.create_table(
        'required_channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('invite_url', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('bot_status', sa.String(length=64), nullable=False, server_default='unknown'),
        sa.Column('last_bot_check', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_required_channels_chat_id', 'required_channels', ['chat_id'], unique=True)

    # 6. settings
    op.create_table(
        'settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key')
    )

    # 7. audit_logs
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('admin_username', sa.String(length=128), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('audit_logs')
    op.drop_table('settings')
    op.drop_table('required_channels')
    op.drop_table('cache_entries')
    op.drop_table('job_requests')
    op.drop_table('jobs')
    op.drop_table('users')
