"""AI 사용량 장부 영속화

Revision ID: 53a7d91c4f20
Revises: a3f8c2e91d47
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "53a7d91c4f20"
down_revision = "a3f8c2e91d47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_request_windows",
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("endpoint", sa.String(length=60), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "endpoint"),
    )
    op.create_table(
        "ai_token_daily",
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("spent_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "usage_date"),
    )
    op.create_table(
        "ai_token_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_token_reservations_user_day", "ai_token_reservations", ["user_id", "usage_date"]
    )
    op.create_index("ix_ai_token_reservations_expires", "ai_token_reservations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_token_reservations_expires", table_name="ai_token_reservations")
    op.drop_index("ix_ai_token_reservations_user_day", table_name="ai_token_reservations")
    op.drop_table("ai_token_reservations")
    op.drop_table("ai_token_daily")
    op.drop_table("ai_request_windows")
