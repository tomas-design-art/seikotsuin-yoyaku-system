"""autopilot_min_duration_minutes 30 -> 20

LINE自動予約で自動に予約してよい施術時間の下限を20分にする（正解の一覧 X6・まことさん 2026-10-07）。
既定値の 30 のまま残っているときだけ 20 に直す。院が画面で別の値に変えていたら触らない。

Revision ID: 031_autopilot_min_duration_20
Revises: 030_line_autopilot_logs
Create Date: 2026-10-07
"""
from alembic import op


revision = "031_autopilot_min_duration_20"
down_revision = "030_line_autopilot_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE settings SET value = '20' "
        "WHERE key = 'autopilot_min_duration_minutes' AND value = '30'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings SET value = '30' "
        "WHERE key = 'autopilot_min_duration_minutes' AND value = '20'"
    )
