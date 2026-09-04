"""Shared pytest fixtures."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_session
from app.main import app, require_api_key
from app.models.enums import ComplianceMode, DocumentType, IngestionSource, TransactionType
from app.models.orm import Document, Instrument


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine
    engine.dispose()


@pytest.fixture()
def session(db):
    s = db[0]()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def client(tmp_path):
    """HTTP client with dependency overrides -> an isolated scratch DB, auth
    mocked to accept a fixed key. The real auth-gate *logic* is tested directly
    in test_api.py."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_get_session():
        with TestSessionLocal() as session:
            yield session

    def _override_require_api_key(key=None):
        return "test-key-123"

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[require_api_key] = _override_require_api_key
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def make_instrument(txn_type: TransactionType = TransactionType.LOAN,
                    mode: ComplianceMode = ComplianceMode.TRADITIONAL,
                    **overrides) -> Instrument:
    defaults = dict(
        id=f"inst-{txn_type.value}",
        transaction_type=txn_type,
        compliance_mode=mode,
        issuer_name="Test Issuer PLC",
        issuer_type="Corporate",
        amount=1_000_000.0,
        currency="USD",
        type_specific_data={},
    )
    defaults.update(overrides)
    return Instrument(**defaults)


def make_document(instrument, *, doc_type=DocumentType.UNCLASSIFIED,
                  source=IngestionSource.NATIVE_EXTRACTION, **overrides) -> Document:
    defaults = dict(
        id=f"doc-{instrument.id}",
        instrument_id=instrument.id,
        filename="source.txt",
        file_url=f"mem://{instrument.id}/source.txt",
        document_type=doc_type,
        classification_confidence=0.95,
        extraction_confidence=0.9,
        extraction_schema_name="",
        extraction_schema_version="",
        ingestion_source=source,
        compliance_mode=instrument.compliance_mode,
        status="uploaded",
        extracted_data={},
    )
    defaults.update(overrides)
    return Document(**defaults)
