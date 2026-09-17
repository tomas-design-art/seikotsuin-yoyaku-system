"""add hotpepper_sync_round to reservations

予約を転記後に別の時間・担当へ動かしたら、RPA に「番号-回数」で渡し直すための回数。

Revision ID: 029_reservation_sync_round
Revises: 028_line_reservation_source_ref
Create Date: 2026-09-17
"""
from alembic import op
import sqlalchemy as sa


revision = "029_reservation_sync_round"
down_revision = "028_line_reservation_source_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("hotpepper_sync_round", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("reservations", "hotpepper_sync_round")
