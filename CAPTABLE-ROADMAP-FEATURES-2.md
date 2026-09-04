# Cap Table — Feature Specs (3–5: SAFE, Waterfall, E-signature)

*Companion to CAPTABLE-ROADMAP.md and CAPTABLE-ROADMAP-FEATURES.md.*

---

## Feature 3: SAFE conversion math

### What
A **SAFE** (Simple Agreement for Future Equity) converts cash into preferred shares at the next priced round. Terms:
- **Valuation cap**: converts as if company valued at cap → more shares if cap < round-implied price.
- **Discount rate**: converts at discount to round price (e.g. 20% off).
- SAFE picks whichever gives **MORE shares** (investor-favorable).

Formula: `conversion_price = min(valuation_cap / fully_diluted, round_price * (1 - discount))`; shares = `investment_amount / conversion_price`.

### Model changes
Extend `Security` (nullable, `security_type == safe` only):

```
Security:
  + valuation_cap: float | None        # e.g. 5_000_000
  + discount_rate: float | None        # e.g. 0.20
  + investment_amount: float | None    # cash via the SAFE
```

`CONVERSION` event gains: `new_round_price_per_share: float`.

### Computation
At a priced round, ALL SAFEs convert. Per SAFE: `effective_price = min(cap / fully_diluted, round_price * (1 - discount))`; `shares = investment_amount / effective_price`. CONVERSION event records source=SAFE, target=preferred, quantity, price. Existing shareholders diluted at round's effective date via event replay.

### API
- Create `safe` Security → accept cap, discount, investment_amount
- `POST /cap-table-events` CONVERSION → SAFE→preferred, math enforced server-side

### Tests
- Cap only → cap-based price; discount only → discount price; both → lower price (more shares)
- Conversion dilutes existing shareholders
- Multiple SAFEs at once
- Point-in-time before vs after → correct dilution at each date
- investment_amount = 0 → error

### Complexity: HIGH. Fully-diluted computation at conversion moment, composing with vesting, event replay with price lookup at any as_of.

---

## Feature 4: Exit waterfall modeling

### What
In a liquidity event, proceeds distribute by seniority + liquidation preference — not equally.

### Math (standard venture terms)
- **Liquidation preference** (e.g. 1× investment): preferred paid first.
- **Participating** preferred: gets preference AND pro-rata share of remainder (double-dip).
- **Non-participating** preferred: takes GREATER of (preference, as-converted-to-common) — standard today.
- **Seniority**: multiple preferred series paid in stack order.
- **Common**: gets whatever remains.

### Model changes
Extend `Security` (nullable, preferred-only):

```
Security:
  + issue_price_per_share: float | None
  + liquidation_preference_multiple: float | None   # e.g. 1.0, 2.0
  + is_participating: bool | None
  + seniority_rank: int | None                      # 0 = most senior
```

### Computation
`compute_waterfall(issuer_name, exit_value) -> WaterfallResult`: read-only, per-holder + per-security distribution. Order: seniority stack → preference → participation (if any) → common remainder.

### API
`GET /cap-table/{issuer}/waterfall?exit_value=100000000` → full breakdown. Pure query, no events written.

### Scope decision
Implement **non-participating + seniority stack** fully (90% of venture deals). Participating = documented extension. Capped participation = out of scope.

### Tests
- Exit below preference → preferred takes all
- Exit well above → non-participating converts to common (better deal)
- Seniority stack → paid in correct order
- Common gets remainder (or zero if preferences exhaust proceeds)
- Unconverted SAFEs → convert first, then waterfall

### Complexity: VERY HIGH. Many cases; scope kept disciplined.

---

## Feature 5: E-signature

### Pragmatic scope
Real DocuSign/HelloSign = large vendor integration → **deferred**. MVP = a signature record with an integrity hash (proves document wasn't altered after signing). Unsigned events excluded from cap table computation.

### Model

```
Signature:
  id: str
  signable_type: str          # "cap_table_event", "option_grant"
  signable_id: str
  signer_holder_id: str
  signed_at: datetime
  signature_hash: str         # hash of document content at signing (integrity proof)
  method: str                 # "click_to_sign", "docusign" (when integrated)
```

### API
`POST /signatures` → record signature. Events show signed/unsigned. Unsigned events excluded from `compute_cap_table`.

### Tests
- Issuance unsigned → not counted in cap table
- Issuance signed → counted
- Signature hash changes if underlying data tampered (integrity)

### Complexity: MEDIUM (pragmatic scope). Real vendor integration = separate, clearly-scoped project.