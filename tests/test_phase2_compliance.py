"""Phase 2 — compliance-as-configuration: one gateway, two rule sets."""

import pytest

from app.compliance import (
    ComplianceGateway,
    RULE_SETS,
    RuleSeverity,
    run_gateway_with_ledger,
)
from app.models.enums import (
    ComplianceMode,
    DocumentType,
    ShariahContractType,
    ShariahReviewStatus,
    TransactionType,
)
from app.telemetry import clear_traces, find_trace
from tests.conftest import make_document, make_instrument


@pytest.fixture(autouse=True)
def _clean_traces():
    clear_traces()
    yield


def test_traditional_clean_passes_not_applicable(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.LOAN,
        mode=ComplianceMode.TRADITIONAL,
        type_specific_data={"interest_rate": 0.06},
    )
    session.add(instr)
    session.flush()
    docs = [make_document(instr, doc_type=DocumentType.KYC)]

    decision = ComplianceGateway().evaluate(instr, docs)
    assert decision.outcome == ShariahReviewStatus.NOT_APPLICABLE
    assert not decision.blocking
    session.close()


def test_traditional_flagged_for_missing_kyc(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.LOAN,
        mode=ComplianceMode.TRADITIONAL,
        type_specific_data={"interest_rate": 0.06},
    )
    session.add(instr)
    session.flush()
    # No KYC document attached -> blocking
    decision = ComplianceGateway().evaluate(instr, [])
    assert decision.outcome == ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT
    assert any(f.code == "TRAD_KYC_MISSING" for f in decision.findings)
    session.close()


def test_traditional_flagged_for_out_of_band_rate(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.LOAN,
        mode=ComplianceMode.TRADITIONAL,
        type_specific_data={"interest_rate": 0.95},
    )
    session.add(instr)
    session.flush()
    docs = [make_document(instr, doc_type=DocumentType.KYC)]
    decision = ComplianceGateway().evaluate(instr, docs)
    assert decision.outcome == ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT
    assert any(f.code == "TRAD_RATE_OUT_OF_BAND" for f in decision.findings)
    session.close()


def test_islamic_clean_lands_pending_not_approved(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.SUKUK,
        mode=ComplianceMode.ISLAMIC,
        shariah_contract_type=ShariahContractType.MURABAHA,
        underlying_asset_id="asset-001",
        underlying_asset_description="Commodity basket",
        type_specific_data={"profit_rate": 0.05},
    )
    session.add(instr)
    session.flush()
    docs = [make_document(instr, doc_type=DocumentType.FATWA)]

    decision = ComplianceGateway().evaluate(instr, docs)
    assert decision.outcome == ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
    assert not decision.blocking
    session.close()


def test_islamic_fixed_interest_flagged_automatically(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.SUKUK,
        mode=ComplianceMode.ISLAMIC,
        shariah_contract_type=ShariahContractType.IJARA,
        underlying_asset_id="asset-002",
        type_specific_data={"interest_rate": 0.07},  # forbidden on Islamic track
    )
    session.add(instr)
    session.flush()
    docs = [make_document(instr, doc_type=DocumentType.FATWA)]

    decision = ComplianceGateway().evaluate(instr, docs)
    assert decision.outcome == ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT
    assert any(f.code == "SHAB_FIXED_INTEREST" for f in decision.findings)
    session.close()


def test_gateway_emits_trace(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.SUKUK,
        mode=ComplianceMode.ISLAMIC,
        shariah_contract_type=ShariahContractType.MURABAHA,
        underlying_asset_id="a",
        type_specific_data={"profit_rate": 0.05})
    session.add(instr)
    session.flush()
    docs = [make_document(instr, doc_type=DocumentType.FATWA)]
    run_gateway_with_ledger(session, instr, docs)
    trace = find_trace("compliance.gateway")
    assert trace is not None, "gateway decision must emit a telemetry trace"
    assert trace.metadata["rule_set"] == "islamic"
    session.close()


def test_never_approves():
    instr = make_instrument(txn_type=TransactionType.SUKUK, mode=ComplianceMode.ISLAMIC,
                            shariah_contract_type=ShariahContractType.IJARA,
                            underlying_asset_description="x")
    docs = []
    decision = ComplianceGateway().evaluate(instr, docs)
    assert decision.outcome not in {
        ShariahReviewStatus.SCHOLAR_APPROVED,
        ShariahReviewStatus.SCHOLAR_REJECTED,
    }