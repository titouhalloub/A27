# A27 — Honest Positioning

*One page. Shipped features mapped to code. Gaps acknowledged. Roadmap separated from what's built.*

---

## What A27 is

A27 is the **shared pipeline underneath** private-capital operations — document intake, extraction, compliance, and the unified ledger that feeds cap tables and portfolio views. It is not (yet) a cap-table product, an LP portal, or a reporting engine. It is the layer that produces clean, compliance-checked data so those layers don't have to.

---

## Genuine differentiators (built, tested, in `main`)

| Differentiator | Where in code | Why competitors don't |
|---|---|---|
| **Compliance-as-configuration** — one gateway, rule sets as data; traditional + Islamic run the identical code path | `app/compliance.py` — `ComplianceGateway.evaluate`, `TRADITIONAL_RULES` / `ISLAMIC_RULES` | Carta / Allvue / Arch either skip compliance or do it as per-client custom consulting. No category-standard platform runs both tracks in one workflow. |
| **Islamic + Traditional in one workflow** | Same gateway, `compliance_mode` selects the rule set | None of the named competitors offer Shariah compliance at all. Real differentiator for GCC/MENA. |
| **Confidence-gated extraction that never guesses** | `app/classification.py` (gate 0.75), `app/extraction.py` (gate 0.85), heuristic fallback | Below threshold → `UNCLASSIFIED` → human triage. Never silently accepted. |
| **Cross-fund portfolio visibility by design** | `GET /investors/{id}/portfolio` — one investor across instruments/funds, per-track exposure totals | Competitors are per-fund islands. A27 unifies by architecture, not by bolting on a reporting layer. |
| **Event-sourced cap table** — ownership computed, never hand-edited | `app/captable.py` — `compute_cap_table` replays the append-only event log; overdrafts rejected at write | Spreadsheet-backed competitors can drift; A27's answer is always derived from the same events. |
| **No self-certification — enforced at the ORM level** | `app/models/orm.py` — `@validates` blocks `scholar_approved` from any system write; human review requires `reviewer_id` | A structural guarantee, not a policy comment. |
| **Unified audit ledger** | `app/models/orm.py` — `LedgerEntry`, one row per decision, `trace_id` links to telemetry | One data model, every decision traceable. |
| **Real-document intake** — PDF text layer, Tesseract OCR fallback for scans/images, `.txt`/`.md`/`.html` | `app/ocr.py` — `text_from_upload`, streaming 15 MB cap, guaranteed cleanup | Ingests the actual documents ops teams receive, not re-typed numbers. |

---

## Honest gaps (what competitors do that A27 does not — yet)

| Capable competitor | What they do | A27 status |
|---|---|---|
| **Carta** | Vesting schedules, 409A valuations, waterfall modeling, e-signature, cap-table depth | A27's cap table is MVP-grade (event-sourced, correct under dilution/transfers/exercises). No vesting, no 409A, no waterfall, no e-signature. |
| **Arch** | LP portal, document distribution, capital-call processing, investor onboarding | A27 has a demo page, not a portal. No capital-call workflow. |
| **Allvue** | Cross-fund reporting, LP reporting, data aggregation, integrations | A27 has the portfolio *view* (one investor), not a reporting engine or data warehouse. No integrations. |
| **All of the above** | Per-user auth, multi-tenancy, institutional SLAs | A27 has a shared API key. Fine for pilot; not production-grade. |

**Roadmap-only (not built):** capital-call orchestration, on-chain registry / tokenization, quarterly reporting, risk-scoring ML models. These are sequenced after the lending + compliance core is proven with real customers.

---

## The pitch (defensible)

> Carta gives you a beautiful cap table — but you're still typing the numbers in from PDFs. Arch gives you a beautiful LP portal — but someone still reads every subscription document for AML by hand. Allvue aggregates data across funds — but it needs clean data from other systems to do it.
>
> **A27 owns the messy upstream:** intake the PDF, extract it with confidence, run compliance (traditional AND Islamic — same engine, configured not coded), and feed a unified ledger that powers the cap table and the cross-fund portfolio view. We're the layer underneath those platforms. And the Islamic-traditional-in-one-workflow piece? None of them do it.

---

## What to claim vs. what to avoid

| ✅ Claim (it's real) | ❌ Don't claim (it's not built) |
|---|---|
| One compliance engine, both tracks, configured not coded | Capital-call automation |
| Cross-fund portfolio view by design | Tokenization / on-chain registry |
| Confidence-gated extraction, never guesses | Quarterly reporting / data warehouse |
| Event-sourced cap table, overdraft-safe | Vesting / 409A / waterfall modeling |
| No self-certification, ORM-enforced | Per-user auth / multi-tenancy |
| Real-document intake (PDF, scan, image) | LP portal / document distribution |
| Unified audit ledger, every decision traceable | Risk-scoring ML models |

---

## Architecture at a glance

```
PDF / scan / txt / image
        │
        ▼
   ┌─────────┐     confidence-gated     ┌─────────────┐
   │  OCR /   │ ───────────────────────► │  Classify   │  below 0.75 → human triage
   │  text    │                          │  (0.75 gate)│
   │  extract │                          └──────┬──────┘
   └─────────┘                                 │
                                               ▼
                                        ┌─────────────┐
                                        │  Extract    │  typed schema, versioned
                                        │  (0.85 gate)│
                                        └──────┬──────┘
                                               │
                                               ▼
                                   ┌───────────────────────┐
                                   │  Compliance Gateway   │  ◄── rule set = config
                                   │  (traditional | islamic)│     (not a fork)
                                   └───────────┬───────────┘
                                               │
                              ┌────────────────┼────────────────┐
                              ▼                ▼                ▼
                         not_applicable   pending_scholar   system_flagged
                              │                │                │
                              └────────────────┼────────────────┘
                                               ▼
                                   ┌───────────────────────┐
                                   │  Human Review         │  ◄── ONLY path to
                                   │  (reviewer_id req'd)  │     scholar_approved
                                   └───────────┬───────────┘
                                               │
                                               ▼
                                   ┌───────────────────────┐
                                   │  Unified Ledger       │  append-only, trace_id
                                   │  Cap Table (replayed) │  event-sourced
                                   │  Portfolio (cross-fund)│ by design
                                   └───────────────────────┘
```

---

*Prepared for technical conversation. Shipped features are on `main` and covered by 62 passing tests. Roadmap items are sequenced, not shipped.*