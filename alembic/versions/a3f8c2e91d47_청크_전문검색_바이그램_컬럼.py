"""청크 전문검색 바이그램 컬럼

Revision ID: a3f8c2e91d47
Revises: 207f00d842f2
Create Date: 2026-08-25

document_chunks.text_tsv — 한국어 어휘 검색용 바이그램 tsvector.
셔글 계산은 파이썬(app.rag.lexical)이 하므로 generated 표현식이 아니라
애플리케이션이 저장 시 채운다. 기존 행은 백필로 채운다:

    python -m app.rag.search --tsv-backfill
"""


from alembic import op

revision: str = "a3f8c2e91d47"
down_revision: str | None = "ee8e10ddd35b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # IF NOT EXISTS — 워크트리 여러 개가 같은 호스트 DB를 공유한다.
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS text_tsv tsvector")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_tsv ON document_chunks USING gin (text_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS text_tsv")
