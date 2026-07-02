# Market Mass v0.4 Dashboard Layer

Generated: 2026-06-27. Research only; not financial advice.

## Status

Status: `market_mass_v0.4_dashboard_layer`

This is the visualization and portfolio-integration layer for the market-mass
model. It should not be labeled v0.5; v0.5 remains reserved for execution-realism
evidence from real option-chain snapshot replay.

## Scope

Implemented dashboard behavior:

- Anchor-first release mode with `--anchor-only`.
- QQQ and VOO as tradable anchors.
- `^NDX` and `^GSPC` as reference lanes only.
- 130-bar replay for price, center of mass, boundary zones, quality, build-up,
  distance-z, and regime.
- Per-symbol freshness fields: `priceAsOf`, `massAsOf`, `historyEnd`, `stale`,
  and `staleReason`.
- Per-symbol `dashboardConfidence`, separate from center quality.
- Compact per-stock badge that avoids buy/sell language and labels proxy-vol
  cases.

## Guardrails

- Boundary zones are probabilistic ranges, not guaranteed support/resistance.
- QQQ-dollar charts use QQQ data only.
- VOO-dollar charts use VOO data only.
- Index reference lanes are not silently converted into ETF strike levels.
- Single-stock boundaries are lower-confidence in the free-data version because
  they use broad-market volatility proxy data rather than single-name IV.

## Validation

Targeted market-mass validation passed:

```text
python3 -m py_compile scripts/market_mass_dashboard.py scripts/market_mass_boundaries.py generate.py
python3 -m unittest discover -s tests -p 'test_market_mass_dashboard.py'
python3 -m unittest discover -s tests -p 'test_market_mass_boundaries.py'
python3 generate.py --no-fetch
node --check /tmp/portfolio_dashboard_script.js
```

Visual smoke checks passed for desktop and mobile screenshots of the
`重心边界` tab.

Full-suite status remains separate: `python3 -m unittest discover -s tests`
currently has one unrelated AICS delta failure in
`test_aics_tool.AicsToolTests.test_history_snapshots_drive_one_week_and_one_month_deltas`.
