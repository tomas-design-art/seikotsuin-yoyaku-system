"""add rpa_call_logs table

Revision ID: 024_rpa_call_logs
Revises: 023_add_synced_by
Create Date: 2026-06-14
"""

from alembic import op
import sqlalchemy as sa


revision = "024_rpa_call_logs"
down_revision = "023_add_synced_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rpa_call_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("endpoint", sa.String(length=200), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("query_params", sa.JSON(), nullable=True),
        sa.Column("body_summary", sa.JSON(), nullable=True),
        sa.Column("response_count", sa.Integer(), nullable=True),
        sa.Column("response_ids", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
    )
    op.create_index(
        "ix_rpa_call_logs_timestamp", "rpa_call_logs", ["timestamp"], unique=False
    )
    op.create_index(
        "ix_rpa_call_logs_endpoint", "rpa_call_logs", ["endpoint"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rpa_call_logs_endpoint", table_name="rpa_call_logs")
    op.drop_index("ix_rpa_call_logs_timestamp", table_name="rpa_call_logs")
    op.drop_table("rpa_call_logs")
