"""upgrade cap table to the full event-sourcing schema

Adds target_security_id / from_holder_id / notes / recorded_at to
cap_table_events, makes holder_id nullable (cancellations have no
recipient), moves quantity/authorized_shares to Float, widens the
security_type vocabulary to the full SecurityType set, aligns indexes
with app.models.orm, and indexes securities.issuer_name.

Revision ID: a27cap000003
Revises: a27cap000002
"""
import sqlalchemy as sa
from alembic import op

revision = "a27cap000003"
down_revision = "a27cap000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("securities") as batch:
        batch.add_column(sa.Column("par_value", sa.Float(), nullable=True))
        batch.alter_column(
            "authorized_shares",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=False,
        )
        batch.alter_column(
            "security_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=24),
            existing_nullable=False,
        )
    op.create_index(
        op.f("ix_securities_issuer_name"), "securities", ["issuer_name"], unique=False
    )

    with op.batch_alter_table("cap_table_events") as batch:
        batch.add_column(
            sa.Column("target_security_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("from_holder_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "recorded_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.drop_column("created_at")
        batch.alter_column(
            "holder_id",
            existing_type=sa.String(length=64),
            nullable=True,
            existing_nullable=False,
        )
        batch.alter_column(
            "event_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch.alter_column(
            "quantity",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=False,
        )
        batch.alter_column(
            "effective_date",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
        batch.drop_index("ix_cap_table_event_security")
        batch.drop_index("ix_cap_table_event_holder")
        batch.drop_index("ix_cap_table_event_effective")
        batch.create_index("ix_captable_security_id", ["security_id"], unique=False)
        batch.create_index(
            "ix_captable_effective_date", ["effective_date"], unique=False
        )
        batch.create_foreign_key(
            "fk_cap_table_events_target_security_id",
            "securities",
            ["target_security_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_cap_table_events_from_holder_id",
            "investors",
            ["from_holder_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("cap_table_events") as batch:
        batch.drop_foreign_key("fk_cap_table_events_from_holder_id")
        batch.drop_foreign_key("fk_cap_table_events_target_security_id")
        batch.drop_index("ix_captable_effective_date")
        batch.drop_index("ix_captable_security_id")
        batch.create_index(
            "ix_cap_table_event_effective", ["effective_date"], unique=False
        )
        batch.create_index("ix_cap_table_event_holder", ["holder_id"], unique=False)
        batch.create_index(
            "ix_cap_table_event_security", ["security_id"], unique=False
        )
        batch.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.drop_column("recorded_at")
        batch.drop_column("notes")
        batch.drop_column("from_holder_id")
        batch.drop_column("target_security_id")
        batch.alter_column(
            "holder_id",
            existing_type=sa.String(length=64),
            nullable=False,
            existing_nullable=True,
        )
        batch.alter_column(
            "quantity",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch.alter_column(
            "event_type",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch.alter_column(
            "effective_date",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
        )

    op.drop_index(op.f("ix_securities_issuer_name"), table_name="securities")
    with op.batch_alter_table("securities") as batch:
        batch.drop_column("par_value")
        batch.alter_column(
            "authorized_shares",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch.alter_column(
            "security_type",
            existing_type=sa.String(length=24),
            type_=sa.String(length=32),
            existing_nullable=False,
        )