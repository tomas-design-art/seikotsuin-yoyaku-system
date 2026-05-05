"""add daily report code to practitioners

Revision ID: 021_practitioner_daily_report_code
Revises: 020_audit_logs
Create Date: 2026-05-05
"""

from alembic import op
import sqlalchemy as sa


revision = "021_practitioner_daily_report_code"
down_revision = "020_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("practitioners", sa.Column("daily_report_code", sa.String(length=4), nullable=True))
    op.execute("UPDATE practitioners SET daily_report_code = '上' WHERE name = '上田'")
    op.execute("UPDATE practitioners SET daily_report_code = '出' WHERE name = '出口'")
    op.execute("UPDATE practitioners SET daily_report_code = '時' WHERE name = '時田'")


def downgrade() -> None:
    op.drop_column("practitioners", "daily_report_code")
