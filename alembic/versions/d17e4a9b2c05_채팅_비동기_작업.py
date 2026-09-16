"""채팅 비동기 작업 테이블 (#90).

Revision ID: d17e4a9b2c05
Revises: b48f6e02d91c
Create Date: 2026-09-16 15:00:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d17e4a9b2c05"
down_revision = "b48f6e02d91c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("conversation_id", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'completed', 'failed')", name="ck_chat_jobs_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_jobs_status_created", "chat_jobs", ["status", "created_at"])
    op.create_index(
        "ix_chat_jobs_idempotency", "chat_jobs", ["user_id", "idempotency_key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_chat_jobs_idempotency", table_name="chat_jobs")
    op.drop_index("ix_chat_jobs_status_created", table_name="chat_jobs")
    op.drop_table("chat_jobs")
