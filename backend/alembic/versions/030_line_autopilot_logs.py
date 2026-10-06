"""add line_autopilot_logs

LINE自動予約の記録。患者からの1通ごとに、文・前後の会話状態・AIの読み取り・送った返信・
予約の前後を残す。監査ログとは別の表にする。

Revision ID: 030_line_autopilot_logs
Revises: 029_reservation_sync_round
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa


revision = "030_line_autopilot_logs"
down_revision = "029_reservation_sync_round"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "line_autopilot_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("line_user_id", sa.String(length=100), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=True),
        sa.Column("webhook_event_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("received_text", sa.Text(), nullable=True),
        sa.Column("mode_before", sa.String(length=50), nullable=True),
        sa.Column("mode_after", sa.String(length=50), nullable=True),
        sa.Column("state_before", sa.JSON(), nullable=True),
        sa.Column("state_after", sa.JSON(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("reservations_before", sa.JSON(), nullable=True),
        sa.Column("reservations_after", sa.JSON(), nullable=True),
        sa.Column("reservation_changes", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_line_autopilot_logs_created_at", "line_autopilot_logs", ["created_at"])
    op.create_index("ix_line_autopilot_logs_line_user_id", "line_autopilot_logs", ["line_user_id"])
    op.create_index("ix_line_autopilot_logs_patient_id", "line_autopilot_logs", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_line_autopilot_logs_patient_id", table_name="line_autopilot_logs")
    op.drop_index("ix_line_autopilot_logs_line_user_id", table_name="line_autopilot_logs")
    op.drop_index("ix_line_autopilot_logs_created_at", table_name="line_autopilot_logs")
    op.drop_table("line_autopilot_logs")
