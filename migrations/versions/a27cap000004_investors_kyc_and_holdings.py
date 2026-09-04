"""add investors kyc flag and holdings for cross-fund portfolio

Adds investors.kyc_verified, narrows investors.investor_type to the
InvestorType vocabulary (individual | institution | fund), and creates the
holdings join table that lets one investor's portfolio span instruments
from many funds and both compliance tracks.

Revision ID: a27cap000004
Revises: a27cap000003
"""
import sqlalchemy as sa
from alembic import op

revision = "a27cap000004"
down_revision = "a27cap000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("investors") as batch:
        batch.add_column(
            sa.Column(
                "kyc_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.alter_column(
            "investor_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=24),
            existing_nullable=False,
        )
    op.create_table(
        "holdings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("investor_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("stake_amount", sa.Float(), nullable=False),
        sa.Column("ownership_percentage", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "EXITED",
                name="holdingstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["investor_id"], ["investors.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_holdings_instrument_id", "holdings", ["instrument_id"], unique=False
    )
    op.create_index(
        "ix_holdings_investor_id", "holdings", ["investor_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_holdings_investor_id", table_name="holdings")
    op.drop_index("ix_holdings_instrument_id", table_name="holdings")
    op.drop_table("holdings")
    with op.batch_alter_table("investors") as batch:
        batch.drop_column("kyc_verified")
        batch.alter_column(
            "investor_type",
            existing_type=sa.String(length=24),
            type_=sa.String(length=32),
            existing_nullable=False,
        )