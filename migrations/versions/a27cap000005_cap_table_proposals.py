"""add cap table proposals: extracted ownership facts awaiting human approval

Revision ID: a27cap000005
Revises: a27cap000004
Create Date: 2026-09-05

A CapTableProposal is NOT a CapTableEvent. It is a candidate ownership
fact extracted from a document, held in PROPOSED status until a named
human approves it. Only approval writes a real CapTableEvent. This is
the same principle as the Shariah-review guard: the system proposes,
a person decides.
"""

import sqlalchemy as sa
from alembic import op


revision = 'a27cap000005'
down_revision = 'a27cap000004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cap_table_proposals',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('document_id', sa.String(length=64), nullable=True),
        sa.Column('instrument_id', sa.String(length=64), nullable=True),
        sa.Column('issuer_name', sa.String(length=255), nullable=False),
        sa.Column('security_name', sa.String(length=255), nullable=False),
        sa.Column('security_type', sa.Enum('COMMON', 'PREFERRED', 'OPTION', 'WARRANT', 'SAFE', 'CONVERTIBLE_NOTE', name='securitytype', native_enum=False, length=24), nullable=False),
        sa.Column('holder_id', sa.String(length=64), nullable=True),
        sa.Column('holder_name', sa.String(length=255), nullable=True),
        sa.Column('event_type', sa.Enum('ISSUANCE', 'TRANSFER', 'CANCELLATION', 'EXERCISE', 'CONVERSION', name='captableeventtype', native_enum=False, length=16), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('price_per_share', sa.Float(), nullable=True),
        sa.Column('effective_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.Enum('PROPOSED', 'APPROVED', 'REJECTED', name='proposalstatus', native_enum=False, length=16), nullable=False),
        sa.Column('proposed_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed_by', sa.String(length=255), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('cap_table_event_id', sa.String(length=64), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['cap_table_event_id'], ['cap_table_events.id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
        sa.ForeignKeyConstraint(['holder_id'], ['investors.id'], ),
        sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_captable_proposals_status', 'cap_table_proposals', ['status'], unique=False)
    op.create_index('ix_captable_proposals_issuer', 'cap_table_proposals', ['issuer_name'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_captable_proposals_issuer', table_name='cap_table_proposals')
    op.drop_index('ix_captable_proposals_status', table_name='cap_table_proposals')
    op.drop_table('cap_table_proposals')
