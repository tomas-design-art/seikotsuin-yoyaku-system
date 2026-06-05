"""add synced_by to reservations

Revision ID: 023_add_synced_by
Revises: 022_settings_value_text
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa


revision = "023_add_synced_by"
down_revision = "022_settings_value_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("synced_by", sa.String(length=10), nullable=True),
    )
    # 既存の synced 済み予約は 'legacy' で埋める。
    # NULL のままだと reconcile-queue が再叩き対象にしてしまうため。
    op.execute(
        "UPDATE reservations SET synced_by = 'legacy' "
        "WHERE hotpepper_synced = TRUE AND synced_by IS NULL"
    )


def downgrade() -> None:
    op.drop_column("reservations", "synced_by")
