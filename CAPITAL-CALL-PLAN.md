# Capital-Call Plan: from extracted notice to governed cash obligation

*Re-scoped around what exists. The Day 1-8 sprint was written before the
cap-table bridge shipped; its "approval routing" days are now redundant
with the proposal gate. This plan reuses that gate instead of building a
second one. No code changes in this commit — plan only.*

---

## 1. What exists today (verified in code, not assumed)

| Piece | Where | State |
|---|---|---|
| `CapitalCallExtraction` schema | `app/schemas.py:92-110` | ✅ funder, currency, owing, due date, wire, source text |
| `extract_capital_call()` | `app/extraction.py:293+` | ✅ regex extractor, wired into dispatch |
| `CapitalCall` model | `app/models/orm.py:400-475` | ✅ owing, committed, amount_due, due_date, wire, status, manual-review flag |
| `CapitalCallStatus` | `app/models/enums.py:173-181` | ✅ PENDING_COMMITMENT_LOOKUP / PENDING_APPROVAL / APPROVED / REJECTED |
| `POST /instruments/{id}/capital-calls` | `app/main.py:~430+` | ✅ creates row, computes amount_due when a Holding matches |
| `GET /instruments/{id}/capital-calls` | `app/main.py` | ✅ lists calls for an instrument |
| `POST /capital-calls/{id}/review` | `app/main.py` | ✅ named-reviewer approve/reject gate (409 guards, ledger) |
| `GET /capital-calls?status=` | `app/main.py` | ✅ review queue filter |
| Proposal gate (equity side) | `d1c12d9` + `de457c9` | ✅ the pattern to reuse, not rebuild |
| Transfer rules / ROFR / board gates | — | ❌ does not exist anywhere |
| Due-date tracking query | — | ❌ `due_date` stored but never queried |
| Payment / reconciliation | — | ❌ no model, no endpoints |
| Outbound CRM push | — | ❌ nothing leaves the system |

Design fact the plan respects: the `CapitalCall` model already encodes
the product principle — *"The system never moves money on its own: an
extracted call is PENDING_APPROVAL until a human reviewer confirms it."*
Calls are obligations, not ownership facts, so they live in their own
table — not in `CapTableProposal`. The reuse is the *gate pattern*
(named reviewer, 409 guards, ledger entries), not the table.

---

## 2. Gap analysis: plan docs vs code vs the Day 1-8 sprint

- `CAPTABLE-ROADMAP.md` (vesting → 409A → SAFE → waterfall → e-sign):
  **untouched, still valid, explicitly deferred** — independent of this
  track, not on the critical path to "unified execution".
- `CAPTABLE-BRIDGE-PLAN.md`: **built, and built bigger than written**
  (`d1c12d9` + `de457c9` + `600fcbf`, plus classifier/extractor fixes
  `3a14be0`/`7ed06e8`/`14251d5`). Its demo payoff (upload → extract →
  "1 proposed event awaits review" → approve → ownership bar animates)
  is live in the UI.
- Day 1-8 sprint (chat-only, no plan doc): Days 1-2 (ingestion +
  extraction) are **done**; Days 3-4 (transfer rules engine) were never
  started and belong to a later workflow-engine phase; Days 5-7
  (approval routing) are **redundant** — the proposal gate and the
  capital-call review endpoint already implement that pattern twice.

---

## 3. Build order (dependency order, smallest shippable first)

### Phase A — Approval parity for capital calls (SMALL)

The equity side has link-investor resolution (`POST
/cap-table-proposals/{id}/link-investor`), a loud UI for fetch failures,
an empty-registry path, and an inline create-investor form (`68becc5`).
The capital-call side has none of that: `_create_capital_call` stores a
raw `funder_id` string with no registry check, and there is no UI
surface for the call queue at all.

- A1: resolve `funder_id` against the `Investor` registry on review
  (match by name; 409 with an actionable message when unresolved —
  same semantics as the subscriber guard, adapted: a call without a
  known funder cannot be approved). A `PROVISIONAL` investor (created
  by extraction) IS linkable and approvable — the flag is a warning
  for the reviewer, not a block. Only a fully unresolved funder
  (no investor row at all) blocks approval.
- A2: UI panel for the call queue (pending calls with amount / currency /
  due date / funder + approve/reject with named reviewer), mirroring the
  proposals panel. No new endpoints needed — `GET /capital-calls` and
  `POST /capital-calls/{id}/review` already exist.
- Tests: unresolved funder blocks approval (409, nothing written);
  approve with resolved funder → `APPROVED` + ledger entry; double
  approve → 409. Mirror `tests/test_captable_from_document.py`.

### Phase B — Due-date tracking (SMALL, read-only)

`due_date` is stored and never queried. Pure-query addition, no model
change, no ownership semantics:

- B1: `GET /capital-calls/overdue?as_of=` — calls with `due_date <
  as_of` and `status == PENDING_APPROVAL`, oldest first.
- B2: surface overdue state in the Phase A panel (badge/count).
- Tests: pending + past-due appears; approved past-due does not;
  future-due does not.

### Phase C — Reconciliation (MEDIUM, one new model)

Matching a payment against an approved call. New model, additive:

```
capital_call_payments:
  id                String(64) PK
  capital_call_id   FK capital_calls.id
  amount            Float
  currency          String(8)
  paid_date         DateTime
  reference         String(255) nullable   # wire ref / evidence doc id
  recorded_by       String(64)             # named human, same audit rule
  created_at        DateTime
```

- C1: model + migration (nullable-safe, same batch-mode convention).
- C2: `POST /capital-calls/{id}/payments` (named `recorded_by`, 409
  unless the call is `APPROVED`; ledger entry per payment) and call
  status derivation (`UNPAID` / `PARTIAL` / `PAID` computed from
  `sum(payments) vs amount_due` — derived, never stored; expose
  `remaining = max(0, amount_due - paid)` alongside the status so the
  UI never recomputes it differently).
- C3: UI: per-call payment list + record-payment form in the Phase A
  panel.
- Tests: partial then full payment → derived status transitions;
  payment on unapproved call → 409; overpayment → 409 (never silently
  absorb extra cash).

### Phase D — Transfer rules engine (LARGE, separate phase, needs its own design)

Bylaws / ROFR / board-approval gates evaluated against proposed
`TRANSFER` events. Explicitly **not** part of this plan beyond this
paragraph: it needs a rule-schema design (condition / gate / approver /
escalation), a storage model, an evaluation point in the event-write
path, and UI for rule authoring. Trigger: start the design doc when a
pilot customer brings concrete transfer-rule requirements — not before.
Write that design doc when Phase C ships. Do not start it here.

---

## 4. Scope boundaries (explicitly OUT)

- Real payment rails / wire execution — record-keeping + reconciliation only.
- E-signature on calls (belongs to roadmap Feature 5, orthogonal).
- Auto-approval / bulk approve — the named-human gate is the product.
- Transfer/ROFR/board logic (Phase D, separate design).
- Vesting / 409A / SAFE / waterfall (cap-table math roadmap, independent).
- Outbound CRM push (Allvue/Carta-style — integration track, not this plan).
- Background jobs / due-date notifications — synchronous + query-only
  until a real scheduler is justified.

---

## 5. Migration + test strategy (same conventions as the bridge)

- Additive only: Phase C adds one table; Phases A/B add no tables.
- One Alembic migration for Phase C, SQLite batch-mode as before.
- Every endpoint gets the same guards the bridge proved: named reviewer
  required, 409 on double-decision, 409 on unresolved counterparty,
  ledger entry per decision, full-flow HTTP tests mirroring
  `tests/test_captable_from_document.py`.
- `tests/test_demo_endpoints.py` must keep passing — any new UI call
  must resolve against a real route.

---

## 6. Demo payoff (when A–C ship)

Upload a capital-call notice → fields extracted with confidence →
**"1 call awaits review"** → link funder → approve → due-date tracked →
record payment → reconciled. Same one-screen story as the bridge payoff,
for cash instead of shares — and together they are the "unified
execution layer" claim made visible twice.
