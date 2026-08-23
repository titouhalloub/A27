"""Compliance-as-configuration gateway.

The thesis being proven: **one gateway, pluggable rule sets** — never a fork.
``TRADITIONAL_RULES`` and ``ISLAMIC_RULES`` are both plain data
configurations fed through the *same* ``ComplianceGateway.evaluate`` code
path. Nothing in the gateway knows about Islam or KYC specifically; it just
runs the rule set it was handed and applies the same outcome logic:

- Any rule that produces a ``blocking`` finding  -> SYSTEM_FLAGGED_NONCOMPLIANT
- Otherwise, if a scholar review is warranted    -> PENDING_SCHOLAR_REVIEW
- Otherwise                                       -> NOT_APPLICABLE

The gateway can never set SCHOLAR_APPROVED / SCHOLAR_REJECTED — those remain
human-only (``app.review``). Every decision emits a Langfuse trace (console
backend in dev) with the inputs, rule set name, per-rule findings and the
outcome, and is persisted to the append-only ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.enums import (
    ComplianceMode,
    DocumentType,
    LedgerEntryType,
    ShariahContractType,
    ShariahReviewStatus,
)
from app.models.orm import Document, Instrument, LedgerEntry
from app.telemetry import get_tracer


class RuleSeverity(str, Enum):
    BLOCKING = "blocking"      # clear violation -> auto-flag noncompliant
    REVIEW = "review"          # needs human eyes, not an auto-pass
    INFO = "info"              # recorded for the human reviewer


@dataclass
class RuleFinding:
    code: str
    severity: RuleSeverity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleContext:
    instrument: Instrument
    documents: Sequence[Document]
    documents_by_type: dict[DocumentType, list[Document]] = field(init=False)

    def __post_init__(self) -> None:
        self.documents_by_type = {}
        for doc in self.documents:
            self.documents_by_type.setdefault(doc.document_type, []).append(doc)


# A rule is a pure function returning findings; configuration == the ordered
# list of (name, rule) pairs, nothing more.
Rule = Callable[[RuleContext], list[RuleFinding]]
NamedRule = tuple[str, Rule]


@dataclass
class ComplianceDecision:
    rule_set_name: str
    compliance_mode: ComplianceMode
    outcome: ShariahReviewStatus  # system-set status only
    findings: list[RuleFinding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.severity == RuleSeverity.BLOCKING for f in self.findings)


def _docs(ctx: RuleContext, doc_type: DocumentType) -> list[Document]:
    return ctx.documents_by_type.get(doc_type, [])


# ---------------------------------------------------------------------------
# Traditional-track rules
# ---------------------------------------------------------------------------


def traditional_kyc_aml_present(ctx: RuleContext) -> list[RuleFinding]:
    """KYC/AML documentation must exist before a deal proceeds."""
    if not _docs(ctx, DocumentType.KYC):
        return [
            RuleFinding(
                code="TRAD_KYC_MISSING",
                severity=RuleSeverity.BLOCKING,
                message="No KYC/AML document attached to the instrument.",
            )
        ]
    return []


def traditional_interest_sanity(ctx: RuleContext) -> list[RuleFinding]:
    """Interest rate sanity bounds. Out-of-band rates are not auto-accepted.

    Model-agnostic: reads ``interest_rate`` from the type_specific_data bag,
    whether the asset is a loan, a bond-like sukuk, or anything else.
    """
    rate = ctx.instrument.type_specific_data.get("interest_rate")
    if rate is None:
        return [
            RuleFinding(
                code="TRAD_RATE_MISSING",
                severity=RuleSeverity.REVIEW,
                message="No interest_rate on record; cannot apply sanity bounds.",
            )
        ]
    if not 0.0 <= float(rate) <= 0.30:
        return [
            RuleFinding(
                code="TRAD_RATE_OUT_OF_BAND",
                severity=RuleSeverity.BLOCKING,
                message=f"Interest rate {rate} outside acceptable band [0, 0.30].",
                detail={"rate": rate, "bounds": [0.0, 0.30]},
            )
        ]
    return []


def traditional_no_license_issue(ctx: RuleContext) -> list[RuleFinding]:
    """Informational: record the compliance mode for audit."""
    return [
        RuleFinding(
            code="TRAD_MODE_INFO",
            severity=RuleSeverity.INFO,
            message=f"Instrument evaluated under {ctx.instrument.compliance_mode.value} track.",
            detail={"compliance_mode": ctx.instrument.compliance_mode.value},
        )
    ]


# ---------------------------------------------------------------------------
# Islamic-track rules (Shariah requirement table)
# ---------------------------------------------------------------------------


def shariah_contract_type_declared(ctx: RuleContext) -> list[RuleFinding]:
    """Every Islamic instrument must declare its Shariah contract type.

    ``NONE`` is not acceptable for sukuk/murabaha/ijara/musharakah — the
    contract type is what triggers the rest of the requirement table.
    """
    if ctx.instrument.shariah_contract_type is None or (
        ctx.instrument.shariah_contract_type == ShariahContractType.NONE
    ):
        return [
            RuleFinding(
                code="SHAR_CONTRACT_TYPE_MISSING",
                severity=RuleSeverity.BLOCKING,
                message="Islamic instrument is missing a declared Shariah contract type.",
            )
        ]
    return []


def shariah_asset_backing(ctx: RuleContext) -> list[RuleFinding]:
    """Asset-backing: sukuk/murabaha/ijara must reference a real asset."""
    has_desc = bool(ctx.instrument.underlying_asset_description)
    has_id = bool(ctx.instrument.underlying_asset_id)
    if not (has_desc or has_id):
        return [
            RuleFinding(
                code="SHAB_ASSET_BACKING_MISSING",
                severity=RuleSeverity.BLOCKING,
                message="Islamic instrument has no underlying asset description or ID.",
            )
        ]
    return [
        RuleFinding(
            code="SHAB_ASSET_PRESENT",
            severity=RuleSeverity.INFO,
            message="Underlying asset is declared for the Islamic instrument.",
        )
    ]


def shariah_no_fixed_interest(ctx: RuleContext) -> list[RuleFinding]:
    """No fixed interest. Returns must be profit/rent-linked.

    Forbidden indicators in the data bag: ``interest_rate`` or
    ``fixed_coupon=True``. Rental/fund-based returns are fine.
    """
    tsd = ctx.instrument.type_specific_data
    forbidden: list[str] = []
    if tsd.get("interest_rate") is not None:
        forbidden.append(f"interest_rate={tsd['interest_rate']}")
    if tsd.get("fixed_coupon") is True:
        forbidden.append("fixed_coupon=True")

    if forbidden:
        return [
            RuleFinding(
                code="SHAB_FIXED_INTEREST",
                severity=RuleSeverity.BLOCKING,
                message="Fixed interest / fixed coupon detected on the Islamic track.",
                detail={"indicators": forbidden},
            )
        ]
    return [
        RuleFinding(
            code="SHAB_NO_FIXED_INTEREST",
            severity=RuleSeverity.INFO,
            message="No fixed-interest indicators found in type_specific_data.",
            detail={
                "type_specific_data": {
                    k: v
                    for k, v in tsd.items()
                    if k
                    in ("interest_rate", "fixed_coupon", "profit_rate", "ijara_rental_rate")
                }
            },
        )
    ]


def shariah_fatwa_reference(ctx: RuleContext) -> list[RuleFinding]:
    """A fatwa document must accompany the instrument.

    The document is evidence the scholar will weigh — it is not the
    certification. Missing evidence blocks routing to approval.
    """
    fatwas = _docs(ctx, DocumentType.FATWA)
    if not fatwas:
        return [
            RuleFinding(
                code="SHAB_FATWA_REFERENCE_MISSING",
                severity=RuleSeverity.BLOCKING,
                message="No FATWA document attached; cannot route for scholar review.",
            )
        ]
    return [
        RuleFinding(
            code="SHAB_FATWA_REFERENCE_PRESENT",
            severity=RuleSeverity.INFO,
            message=f"{len(fatwas)} fatwa document(s) attached for the scholar.",
            detail={"fatwa_document_ids": [d.id for d in fatwas]},
        )
    ]


def shariah_no_speculation(ctx: RuleContext) -> list[RuleFinding]:
    """No excessive uncertainty (gharar) — a blunt MVP heuristic.

    The full Shariah board review is human; the system only auto-flags the
    obvious margin/derivative/future-style indicators.
    """
    tsd = ctx.instrument.type_specific_data
    warning_markers = ["leverage", "margin", "derivative", "future"]
    hits = [
        k
        for k in warning_markers
        if str(tsd.get(k, "")).lower().strip() not in ("", "false", "0")
    ]
    if hits:
        return [
            RuleFinding(
                code="SHAB_GHARAR_INDICATOR",
                severity=RuleSeverity.BLOCKING,
                message=f"Instrument exhibits potential speculative indicators: {hits}.",
                detail={"indicators": hits},
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Rule set registry — the configuration surface
# ---------------------------------------------------------------------------


TRADITIONAL_RULES: list[NamedRule] = [
    ("kyc_aml_present", traditional_kyc_aml_present),
    ("interest_rate_sanity", traditional_interest_sanity),
    ("mode_info", traditional_no_license_issue),
]

ISLAMIC_RULES: list[NamedRule] = [
    ("contract_type_declared", shariah_contract_type_declared),
    ("asset_backing", shariah_asset_backing),
    ("no_fixed_interest", shariah_no_fixed_interest),
    ("fatwa_reference", shariah_fatwa_reference),
    ("no_speculation", shariah_no_speculation),
]

RULE_SETS: dict[str, list[NamedRule]] = {
    ComplianceMode.TRADITIONAL.value: TRADITIONAL_RULES,
    ComplianceMode.ISLAMIC.value: ISLAMIC_RULES,
}


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class ComplianceGateway:
    """One code path; the rule set is the only variable."""

    def __init__(self, rule_sets: dict[str, list[NamedRule]] | None = None):
        self.rule_sets = rule_sets or RULE_SETS

    def evaluate(
        self,
        instrument: Instrument,
        documents: Sequence[Document],
        rule_set_name: str | None = None,
    ) -> ComplianceDecision:
        """Run every rule in the set for the instrument's compliance mode.

        Outcomes are system-set only:
        - blocking finding  -> SYSTEM_FLAGGED_NONCOMPLIANT
        - otherwise         -> PENDING_SCHOLAR_REVIEW (Islamic)
                               or NOT_APPLICABLE (traditional)
        Never SCHOLAR_APPROVED / SCHOLAR_REJECTED.
        """
        mode = instrument.compliance_mode
        selected = rule_set_name or mode.value
        rules = self.rule_sets[selected]

        ctx = RuleContext(instrument=instrument, documents=documents)
        findings: list[RuleFinding] = []
        for _name, rule in rules:
            findings.extend(rule(ctx))

        blocking = any(f.severity == RuleSeverity.BLOCKING for f in findings)

        if mode == ComplianceMode.ISLAMIC:
            outcome = (
                ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT
                if blocking
                else ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
            )
        else:
            outcome = (
                ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT
                if blocking
                else ShariahReviewStatus.NOT_APPLICABLE
            )

        return ComplianceDecision(
            rule_set_name=selected,
            compliance_mode=mode,
            outcome=outcome,
            findings=findings,
        )


gateway = ComplianceGateway()


def run_gateway_with_ledger(
    session: Session,
    instrument: Instrument,
    documents: Sequence[Document],
    rule_set_name: str | None = None,
    trace_id: str | None = None,
) -> ComplianceDecision:
    """Evaluate, persist the outcome on the instrument and write a ledger row.

    *Never* sets SCHOLAR_APPROVED — enforced by the ORM validates guard even
    if a bug tries.
    """
    decision = gateway.evaluate(instrument, documents, rule_set_name)
    trace = get_tracer().span(
        "compliance.gateway",
        rule_set=decision.rule_set_name,
        instrument_id=instrument.id,
        compliance_mode=instrument.compliance_mode.value,
    )
    trace.finish(
        {
            "outcome": decision.outcome.value,
            "blocking": decision.blocking,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in decision.findings
            ],
        },
        confidence=1.0,  # deterministic rule engine — confidence is structural
        model="rule-engine/v1",
        rule_set=decision.rule_set_name,
        compliance_mode=instrument.compliance_mode.value,
    )
    trace.emit()

    instrument.shariah_review_status = decision.outcome
    session.add(
        LedgerEntry(
            id=str(uuid4()),
            entry_type=LedgerEntryType.COMPLIANCE_EVENT,
            instrument_id=instrument.id,
            document_id=next((d.id for d in documents), None),
            trace_id=trace_id or trace._trace.trace_id,
            payload={
                "event": "compliance_gateway",
                "rule_set": decision.rule_set_name,
                "outcome": decision.outcome.value,
                "findings": [
                    {"code": f.code, "severity": f.severity.value, "message": f.message}
                    for f in decision.findings
                ],
            },
        )
    )
    session.commit()
    return decision