"""add cap table

Revision ID: a27cap000002
Revises: 312f6a770f90
Create Date: 2026-08-29 20:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "a27cap000002"
down_revision = "312f6a770f90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("investor_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "securities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("issuer_name", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("security_type", sa.String(length=32), nullable=False),
        sa.Column("authorized_shares", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cap_table_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("security_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("holder_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_per_share", sa.Float(), nullable=True),
        sa.Column("effective_date", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["holder_id"], ["investors.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cap_table_event_security", "cap_table_events", ["security_id"])
    op.create_index("ix_cap_table_event_holder", "cap_table_events", ["holder_id"])
    op.create_index("ix_cap_table_event_effective", "cap_table_events", ["effective_date"])


def downgrade() -> None:
    op.drop_index("ix_cap_table_event_effective", table_name="cap_table_events")
    op.drop_index("ix_cap_table_event_holder", table_name="cap_table_events")
    op.drop_index("ix_cap_table_event_security", table_name="cap_table_events")
    op.drop_table("cap_table_events")
    op.drop_table("securities")
    op.drop_table("investors")