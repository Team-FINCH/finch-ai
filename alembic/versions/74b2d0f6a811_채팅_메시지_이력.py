"""채팅 메시지 이력을 저장한다.

Revision ID: 74b2d0f6a811
Revises: 6e6c116c52e1
Create Date: 2026-09-15 14:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "74b2d0f6a811"
down_revision = "6e6c116c52e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role in ('user', 'assistant')", name="ck_chat_messages_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_messages_conversation",
        "chat_messages",
        ["user_id", "conversation_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_conversation", table_name="chat_messages")
    op.drop_table("chat_messages")
