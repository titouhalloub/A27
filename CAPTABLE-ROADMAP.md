# Cap Table — Advanced Features Roadmap

*Plan first, build one at a time, review with Claude at each step.*

---

## Current state (what we're building on)

The cap table is **event-sourced**: `compute_cap_table()` in `app/captable.py` replays the `CapTableEvent` log (issuance, transfer, cancellation, exercise, conversion) up to any `as_of` date and derives a `CapTableSnapshot`. Pure function — same events + same date always give the same answer. No ownership number is ever stored; it's always computed.

- `Security`: id, issuer_name, name, security_type (common/preferred/option/warrant/safe/convertible_note), authorized_shares
- `CapTableEvent`: security_id, target_security_id, event_type, holder_id, from_holder_id, quantity, price_per_share, effective_date, recorded_at, notes
- `Investor`: id, name, investor_type, kyc_verified

This is solid and tested (62 tests). The five features below each extend it. Build order = dependency order: earlier features are foundations for later ones.

---

## Build order (and why)

| # | Feature | Complexity | Why this position |
|---|---|---|---|
| 1 | Vesting schedules | MEDIUM | Foundation. SAFEs convert to equity that vests; waterfall needs vested vs unvested. |
| 2 | 409A valuations | LOW | Provides FMV context for strike prices; independent of math features. |
| 3 | SAFE conversion math | HIGH | Depends on clean security model + vesting. |
| 4 | Exit waterfall modeling | VERY HIGH | Depends on SAFEs + preferred terms + vesting all being modeled. |
| 5 | E-signature | MEDIUM | Orthogonal to math; most valuable once the full lifecycle exists. |

```
Feature 1 (Vesting) ─────┐
                          ├──► Feature 3 (SAFE conversion) ──► Feature 4 (Waterfall)
Feature 2 (409A) ─────────┘
Feature 5 (E-signature) — orthogonal, built last
```

---

## Scope boundaries (explicitly OUT this phase)

These are real competitor features, deferred with clear rationale:

- **Capped participation** (preferred participation capped at X× investment) — documented extension
- **Pro-rata / follow-on investment rights** — needs investor portal
- **Real DocuSign/HelloSign integration** — vendor project, deferred
- **IPO / multi-class waterfall** — M&A waterfall only for now
- **Tax withholding on exercise** — accounting integration, deferred
- **Regulatory compliance automation** — legal integration, deferred

---

## Migration strategy

Every model change is **additive** (nullable columns, new tables). Existing events have no vesting/SAFE terms → behave as today (fully-vested, non-convertible) by default. All 62 existing tests stay valid. One Alembic migration per feature, each independently reviewable.

---

*Detailed per-feature specs are in [CAPTABLE-ROADMAP-FEATURES.md](CAPTABLE-ROADMAP-FEATURES.md). This is a plan, not a commitment — review with Claude, adjust, then build one feature at a time.*