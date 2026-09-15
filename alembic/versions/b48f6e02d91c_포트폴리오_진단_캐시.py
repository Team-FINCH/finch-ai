"""포트폴리오 진단 캐시를 저장한다.

Revision ID: b48f6e02d91c
Revises: 74b2d0f6a811
Create Date: 2026-09-15 14:10:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b48f6e02d91c"
down_revision = "74b2d0f6a811"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_diagnosis_cache",
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_as_of", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("portfolio_diagnosis_cache")
