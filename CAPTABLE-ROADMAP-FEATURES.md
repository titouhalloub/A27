# Cap Table — Feature Specs (1–2: Vesting & 409A)

*Companion to CAPTABLE-ROADMAP.md.*

---

## Feature 1: Vesting schedules

### What
Employee option grants don't vest immediately. Standard US terms: **4-year vesting, 1-year cliff** — nothing vests for 12 months, then 25% vests at the cliff, then 1/48th monthly for 36 months. Cap table must show **vested vs unvested**: only vested shares count toward ownership and can be transferred; unvested canceled when someone leaves.

### Model changes
Add vesting fields to **ISSUANCE events** (nullable — only option/warrant issuances vest; common/preferred are fully vested):

```
CapTableEvent:
  + vesting_start_date: datetime | None
  + vesting_period_months: int | None   # e.g. 48
  + cliff_months: int | None             # e.g. 12
```

Vesting is per-grant (not per security class) → attached to event to keep event-sourced model clean.

### Computation
`compute_cap_table` becomes **grant-aware**: for each ISSUANCE with vesting, compute `vested_quantity_at(as_of)`: 0 before cliff, then `total_shares * min(1, elapsed / total_period)`. Sum vested per (security, holder) for ownership; unvested shown separately in snapshot.

### API / output
`CapTableSnapshot` gains `vested_positions`, `unvested_shares_by_grant`. GET cap table shows vested ownership % + unvested breakdown.

### Tests
- Full vest → 100% vested; pre-cliff → 0%; mid-vesting → fractional
- Point-in-time (past as_of) → correct vested amount *then*
- Cancel unvested on leaver → unvested removed, vested untouched
- Transfer of unvested → rejected

### Complexity: MEDIUM. Composing grant-aware cancellation + point-in-time as_of is the care.

---

## Feature 2: 409A valuations

### What
US **Section 409A** requires a fair market value (FMV) valuation of common stock (annually, or after a material event). FMV sets option strike prices — options must be granted ≥ FMV or the employee faces tax penalties. Done by a qualified valuation firm, not the company.

### Model changes
New table — record-keeping, no computation:

```
Valuation:
  id: str
  issuer_name: str
  valuation_date: datetime
  price_per_share: float
  valuation_type: enum (409A_FMV | PREFERRED_PRICE_ROUND)
  method: str | None        # e.g. "OPM backsolve", "independent appraisal"
  notes: str | None
  created_at: datetime
```

### API
- `POST /valuations` — record a valuation
- `GET /valuations/{issuer}` — history, newest first
- `GET /valuations/{issuer}/latest?type=409A_FMV` — current FMV

### Usage
- Options issued below latest 409A FMV → flagged/rejected (compliance rule)
- Displayed on cap table as context (FMV per share, last valuation date)
- Used in waterfall as baseline common price if no preferred exists

### Tests
- Record + retrieve latest
- Issue option below FMV → flagged/rejected
- 409A older than 12 months → "stale" warning (nag, not hard block)

### Complexity: LOW. Record-keeping + validation rules.