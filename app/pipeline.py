"""Phase 5 orchestration — one pipeline, configurable compliance, ledger."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.classification import classify_document
from app.compliance import ComplianceGateway
from app.extraction import run_extraction
from app.models.enums import (
    DocumentStatus,
    DocumentType,
    IngestionSource,
    LedgerEntryType,
    ShariahReviewStatus,
)
from app.models.orm import Document, Instrument, LedgerEntry
from app.telemetry import get_tracer


class PipelineResult:
    def __init__(self, document, class_conf, extract_conf, outcome, routed):
        self.document = document
        self.classconf = class_conf
        self.extractconf = extract_conf
        self.outcome = outcome
        self.routed = routed

    def __repr__(self):
        return (f"<PipelineResult doc={self.document.id} "
                f"type={self.document.document_type.value} "
                f"compliance={self.outcome}>")


def process_document(session: Session, instrument: Instrument,
                     gateway: ComplianceGateway, text: str,
                     filename: str = "document.txt") -> PipelineResult:
    """Classify -> extract -> compliance gate -> ledger, one transaction."""

    def make_doc(doc_type, class_conf, extract_conf, source, status):
        return Document(
            id=str(uuid4()),
            instrument_id=instrument.id,
            filename=filename,
            file_url="mem://" + uuid4().hex,
            document_type=doc_type,
            classification_confidence=class_conf,
            extraction_confidence=extract_conf,
            ingestion_source=source,
            compliance_mode=instrument.compliance_mode,
            status=status,
        )

    def ledger(doc, stage, payload):
        session.add(LedgerEntry(
            id=str(uuid4()),
            entry_type=LedgerEntryType.DOCUMENT_RESULT,
            instrument_id=instrument.id,
            document_id=doc.id,
            payload={**payload, "stage": stage},
        ))

    trace = get_tracer().span("pipeline.run",
                              instrument_id=instrument.id,
                              compliance_mode=instrument.compliance_mode.value)

    # 1. Classification: its own step, confidence gate, never a guess.
    classification = classify_document(text)

    if classification.document_type == DocumentType.UNCLASSIFIED:
        doc = make_doc(DocumentType.UNCLASSIFIED, classification.confidence, 0.0,
                       IngestionSource.MANUAL_ENTRY, DocumentStatus.REVIEW_NEEDED)
        session.add(doc)
        ledger(doc, "classification",
               {"outcome": "unclassified", "confidence": classification.confidence})
        session.commit()
        trace.finish({"status": "unclassified",
                      "confidence": classification.confidence})
        trace.emit()
        return PipelineResult(doc, classification.confidence, 0.0,
                              ShariahReviewStatus.NOT_APPLICABLE, True)

    # 2. Extraction: typed -> JSONB with schema name + version (gap #2).
    outcome = run_extraction(text, classification.document_type.value)

    if outcome.extraction is None or outcome.routed_to_review:
        doc = make_doc(classification.document_type, classification.confidence,
                       outcome.confidence, IngestionSource.NATIVE_EXTRACTION,
                       DocumentStatus.REVIEW_NEEDED)
        doc.extracted_data = outcome.extracted_data
        doc.error_message = outcome.error
        session.add(doc)
        ledger(doc, "extraction",
               {"outcome": "review", "confidence": outcome.confidence,
                "schema": outcome.schema_name})
        session.commit()
        trace.finish({"status": "review", "confidence": outcome.confidence})
        trace.emit()
        return PipelineResult(doc, classification.confidence, outcome.confidence,
                              ShariahReviewStatus.NOT_APPLICABLE, True)

    # 3. Compliance gateway: same code path, rule set = configuration.
    doc = make_doc(classification.document_type, classification.confidence,
                   outcome.confidence, IngestionSource.NATIVE_EXTRACTION,
                   DocumentStatus.PROCESSED)
    doc.extracted_data = outcome.extracted_data
    session.add(doc)

    prior_docs = session.query(Document).filter(
        Document.instrument_id == instrument.id,
        Document.id != doc.id,
    ).all()
    decision = gateway.evaluate(instrument, [doc, *prior_docs])
    instrument.shariah_review_status = decision.outcome

    session.add(LedgerEntry(
        id=str(uuid4()),
        entry_type=LedgerEntryType.COMPLIANCE_EVENT,
        instrument_id=instrument.id,
        document_id=doc.id,
        payload={"event": "compliance_gateway",
                 "rule_set": decision.rule_set_name,
                 "outcome": decision.outcome.value,
                 "blocking": decision.blocking},
    ))
    session.commit()

    trace.finish({"status": "processed",
                  "outcome": decision.outcome.value})
    trace.emit()
    return PipelineResult(doc, classification.confidence, outcome.confidence,
                          decision.outcome, False)