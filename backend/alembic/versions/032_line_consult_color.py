"""add the consult color (相談（当日決定）・水色)

LINE自動予約で「相談したい」を選んだ予約（メニューは当日スタッフと決める）の色を足す
（正解の一覧 C11・まことさん 2026-10-07：緑と青の間の水色）。LINE の処理は色コード #38bdf8 で探す
（app.models.reservation_color.CONSULT_COLOR_CODE）。同じ色コードの色がすでにあれば足さない。

Revision ID: 032_line_consult_color
Revises: 031_autopilot_min_duration_20
Create Date: 2026-10-07
"""
from alembic import op


revision = "032_line_consult_color"
down_revision = "031_autopilot_min_duration_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO reservation_colors (name, color_code, display_order, is_default) "
        "SELECT '相談（当日決定）', '#38bdf8', COALESCE(MAX(display_order), 0) + 1, false "
        "FROM reservation_colors "
        "WHERE NOT EXISTS (SELECT 1 FROM reservation_colors WHERE lower(color_code) = '#38bdf8')"
    )


def downgrade() -> None:
    # 予約やメニューが使っていれば消さない（色が外れた予約を作らない）
    op.execute(
        "DELETE FROM reservation_colors c WHERE lower(c.color_code) = '#38bdf8' AND c.name = '相談（当日決定）' "
        "AND NOT EXISTS (SELECT 1 FROM reservations r WHERE r.color_id = c.id) "
        "AND NOT EXISTS (SELECT 1 FROM menus m WHERE m.color_id = c.id)"
    )
