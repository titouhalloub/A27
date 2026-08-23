"""Phase 1 — data models round-trip + no-self-certification guard."""

import pytest

from app.models.enums import (
    ComplianceMode,
    DocumentType,
    IngestionSource,
    TransactionType,
)
from app.models.orm import Instrument, ShariahReviewValidationError
from tests.conftest import make_document, make_instrument

ALL_TRANSACTION_TYPES = list(TransactionType)


@pytest.mark.parametrize("txn_type", ALL_TRANSACTION_TYPES)
def test_instrument_and_document_roundtrip(session, txn_type):
    instr = make_instrument(txn_type=txn_type)
    session.add(instr)
    session.flush()
    doc = make_document(instr, doc_type=DocumentType.FINANCIAL_STATEMENT)
    session.add(doc)
    session.commit()

    session.expire_all()
    loaded = session.get(Instrument, instr.id)
    assert loaded is not None
    assert loaded.transaction_type == txn_type
    assert loaded.compliance_mode == ComplianceMode.TRADITIONAL
    assert loaded.type_specific_data == {}

    docs = list(loaded.documents)
    assert len(docs) == 1
    assert docs[0].id == doc.id
    assert docs[0].document_type == DocumentType.FINANCIAL_STATEMENT
    assert docs[0].ingestion_source == IngestionSource.NATIVE_EXTRACTION


def test_all_models_created_in_fresh_db(db):
    import sqlalchemy as sa

    with db[1].connect() as conn:
        tables = sa.inspect(conn).get_table_names()
    assert {"instruments", "documents", "ledger_entries"} <= set(tables)


def test_schema_version_columns_present(session):
    instr = make_instrument()
    session.add(instr)
    session.flush()
    doc = make_document(
        instr,
        doc_type=DocumentType.LOAN_AGREEMENT,
        extraction_schema_name="LoanExtraction",
        extraction_schema_version="v1",
    )
    session.add(doc)
    session.commit()
    assert doc.extraction_schema_name == "LoanExtraction"
    assert doc.extraction_schema_version == "v1"


def test_instrument_cannot_self_approve(session):
    instr = make_instrument(txn_type=TransactionType.SUKUK,
                            mode=ComplianceMode.ISLAMIC)
    session.add(instr)
    session.commit()
    with pytest.raises(ShariahReviewValidationError):
        instr.shariah_review_status = "scholar_approved"


def test_document_cannot_self_approve(session):
    instr = make_instrument()
    session.add(instr)
    session.flush()
    doc = make_document(instr)
    session.add(doc)
    session.commit()
    with pytest.raises(ShariahReviewValidationError):
        doc.shariah_review_status = "scholar_approved"


def test_system_can_set_pending_and_flagged(session):
    instr = make_instrument(
        txn_type=TransactionType.SUKUK,
        mode=ComplianceMode.ISLAMIC,
        shariah_contract_type="murabaha",
    )
    session.add(instr)
    session.commit()

    instr.shariah_review_status = "pending_scholar_review"
    session.commit()
    assert instr.shariah_review_status.value == "pending_scholar_review"

    instr.shariah_review_status = "system_flagged_noncompliant"
    session.commit()
    assert instr.shariah_review_status.value == "system_flagged_noncompliant"