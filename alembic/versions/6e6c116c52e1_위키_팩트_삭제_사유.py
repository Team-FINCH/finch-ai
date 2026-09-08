"""위키 팩트 삭제 사유를 남긴다.

Revision ID: 6e6c116c52e1
Revises: 9b4c1f6ad2e7
Create Date: 2026-09-08 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "6e6c116c52e1"
down_revision = "9b4c1f6ad2e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wiki_facts", sa.Column("deleted_reason", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("wiki_facts", "deleted_reason")
