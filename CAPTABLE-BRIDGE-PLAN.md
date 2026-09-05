# Cap-Table Bridge Plan: from extracted document to ownership event

## The gap this closes

Today the document pipeline and the cap table are two parallel worlds. A PDF goes
classify → extract → comply → ledger, and then **stops**. The cap table is fed only by
hand-typed API calls. The only thing the two sides share is an unlinked
`issuer_name` string. The product thesis — "documents become governed operational
data that drives the system" — is currently true for compliance and false for ownership.

This plan adds the missing bridge, scoped to the product's core principle:

> **The system never writes ownership-changing facts on its own. It proposes; a human
> approves.** Same philosophy as Shariah review — the ORM makes self-approval
> impossible, and the only path in is a deliberate, attributed human action.

## Design (4 pieces)

### 1. Document type + equity extraction (prerequisite)

- `DocumentType.SUBSCRIPTION_AGREEMENT = "subscription_agreement"` (new enum value).
- New `SubscriptionExtraction` schema + extractor in `app/extraction.py`, mirroring the
  existing Loan/Sukuk pattern (typed Pydantic schema, confidence gate, no guessing):
  - `issuer_name`, `security_name`, `security_type` (mapped onto the existing
    `SecurityType` enum: common/preferred/option/warrant/safe/convertible_note),
  - `holder_name`, `quantity` (shares), `price_per_share`, `effective_date` if stated.
- Classifier keywords updated for subscription language ("subscription",
  "subscriber", "share purchase", "issue ... shares"). Below the 0.75 gate →
  `UNCLASSIFIED` → human triage, exactly as today.

### 2. `CapTableProposal` table (the proposal itself)

New table, new model. A proposal is **not** a `CapTableEvent`; it never touches the
computed cap table until approved and converted.

```
cap_table_proposals:
  id                String(64) PK
  document_id       FK documents.id (nullable — proposals can be hand-made too)
  issuer_name       String(255)          # the issuer the extracted data named
  event_type        Enum(CapTableEventType)   # usually ISSUANCE at first
  payload           JSON                 # security_name, security_type, holder_name,
                                         # quantity, price_per_share, effective_date,
                                         # from_holder_id / target_security_id when known
  status            Enum(PROPOSED / APPROVED / REJECTED / SUPERSEDED), default PROPOSED
  extracted_data    JSON (optional)      # the full extraction, for context
  proposed_at       datetime
  reviewed_by       String(64) nullable  # reviewer identity — required to convert
  reviewed_at       datetime nullable
  event_id          FK cap_table_events.id nullable  # set on approval (audit link)
```

Lifecycle: `PROPOSED → APPROVED` (converts to a real event) or `PROPOSED → REJECTED`.
Approved proposals are immutable in effect — changing ownership means a new proposal.

### 3. Endpoints (thin wrapper, same style as the rest of main.py)

- `POST /instruments/{id}/cap-table-proposals` — internal: the pipeline creates one
  automatically after a successful run when the doc type is subscription/equity and
  extraction confidence ≥ 0.85. Also exposed for manual creation.
- `GET /cap-table-proposals?status=PROPOSED` — the review queue.
- `POST /cap-table-proposals/{id}/review` — body: `{decision: approve|reject,
  reviewer_id, notes?}`. **This is the single deliberate path** (mirrors
  `review.py:submit_human_review_instrument`):
  - `reject` → status REJECTED, ledger entry, done.
  - `approve` → resolve/create the `Security` (by issuer_name + security_name),
    resolve/create the `Investor` (by holder_name), then write the real
    `CapTableEvent` through the same validation every other event goes through
    (write-time `compute_cap_table` replay → overdraft → 400). On success:
    proposal = APPROVED with `event_id` + `reviewed_by` stamped, **one ledger entry
    recording the whole chain** (document → proposal → reviewer → event).

Auto-resolution rules on approve (explicit, not magic):
- Security: match `(issuer_name, security_name)` exactly; create if missing
  (using extracted `security_type`; `authorized_shares` defaults to the issued
  quantity and can be edited later).
- Holder: match `Investor.name` exactly; create if missing (`investor_type`
  defaults to `individual`).
- If extraction was below threshold or ambiguous → **no proposal is created**.
  The system never proposes from a guess.

### 4. Migration + tests

- Migration `a27cap000005_cap_table_proposals` on top of `a27cap000004`
  (single new table + enum values; SQLite-safe batch mode as before).
- Tests:
  - unit: proposal created from a successful subscription extraction; no proposal
    when below confidence gate; approve converts to event and the cap table
    reflects it; reject leaves cap table untouched; approve of a quantity exceeding
    what's cancellable (transfer case) → 400, proposal stays PROPOSED;
  - API: full flow over HTTP — upload subscription doc → proposal in queue →
    approve with reviewer_id → `GET /cap-table/{issuer}` shows the new position;
  - immutability: approving twice → 409.

## Explicit non-goals (this iteration)

- No auto-approval ever, no bulk approve.
- No transfer/ROFR/board-gate logic (that's the workflow engine, later).
- No Issuer entity yet — `issuer_name` string matching stays, but now it's the
  *pipeline itself* that guarantees the name comes from the document, which is the
  honest version of "connected".
- No background jobs — the whole flow stays synchronous.

## Demo payoff

Upload a subscription agreement → fields extracted with confidence → compliance
clears → **"1 proposed cap-table event awaits review"** → approve as reviewer →
the ownership bar animates. One flow, one screen: the unified-platform claim made
visible and true.

## Effort

~1 focused session: enum + schema + extractor (30%), model + migration (15%),
endpoints + conversion logic (30%), tests (25%).
