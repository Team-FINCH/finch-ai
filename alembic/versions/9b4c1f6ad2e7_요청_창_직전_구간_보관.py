"""요청 창 직전 구간 보관

고정 창은 경계에서 한도가 통째로 초기화되어 두 배가 몰려 들어온다. 직전 창의
건수를 함께 들고 겹치는 비율만큼 가중 합산하려고 컬럼을 하나 더한다.

Revision ID: 9b4c1f6ad2e7
Revises: 53a7d91c4f20
Create Date: 2026-08-31
"""

import sqlalchemy as sa

from alembic import op

revision = "9b4c1f6ad2e7"
down_revision = "53a7d91c4f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_request_windows",
        sa.Column("previous_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ai_request_windows", "previous_count")
