"""widen settings.value from VARCHAR(500) to TEXT

処理済みメールIDハッシュ一覧（最大1000件）やJSON等の可変長設定値を保存する際、
VARCHAR(500) を超えて StringDataRightTruncationError が発生していたため TEXT へ拡張する。

Revision ID: 022_settings_value_text
Revises: 021_daily_report_code
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "022_settings_value_text"
down_revision = "021_daily_report_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "settings",
        "value",
        existing_type=sa.String(length=500),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 500文字を超える既存値があると失敗するため USING で切り詰める。
    op.execute("ALTER TABLE settings ALTER COLUMN value TYPE VARCHAR(500) USING left(value, 500)")
