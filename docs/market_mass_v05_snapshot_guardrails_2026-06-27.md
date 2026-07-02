# Market Mass v0.5 Snapshot Guardrails

Generated: 2026-06-27. Research only; not financial advice.

## Objective

Continue from v0.4 snapshot infrastructure by making snapshot replay fail
closed and auditable. The goal is to prevent a future report from saying
"snapshot replay" when most trades silently used synthetic credits.

## Implemented

Backtest guardrails:

- Added `--require-snapshot-fills` / `--no-synthetic-fallback`.
- Added `--min-snapshot-fill-coverage`.
- Added `--max-snapshot-age-minutes`.
- Added `--entry-timestamp-policy` with:
  - `same_day_close`
  - `same_day_open`
  - `next_open`
- Added timestamp-aware snapshot matching.
- Added per-run coverage fields:
  - `pricing_source`
  - `snapshot_fill_coverage_pct`
  - `trades_with_snapshot_entry`
  - `trades_with_snapshot_exit`
  - `trades_falling_back_to_synthetic`
  - `snapshot_rejection_count`
  - `rejections_with_snapshot_fill`
  - `avg_snapshot_age_minutes`
  - `max_snapshot_age_minutes`
  - `snapshot_coverage_gate_pass`
- Added `snapshot_replay_rejections.csv`.
- Added `snapshot_replay_rejections.json`.

Archiver guardrails:

- Normalized CSV still writes under the existing partition.
- Raw yfinance rows are now preserved by default.
- Metadata JSON is now written by default.

Dry-run result:

```text
Archived 183 option rows for QQQ at 2026-06-27T03:26:59-04:00
dry-run:data/option_chain_snapshots/QQQ/2026/06/2026-06-27_032659_ET_QQQ_2026-06-29.csv
dry-run:data/option_chain_snapshots/QQQ/raw/2026-06-27/2026-06-27_032659_ET_QQQ_2026-06-29_raw.json
dry-run:data/option_chain_snapshots/QQQ/metadata/2026-06-27/2026-06-27_032659_ET_QQQ_2026-06-29_metadata.json
```

## Interpretation

This does not prove Candidate A or Candidate B with real fills. It makes the
next replay harder to fool:

- Missing expiries become explicit rejections.
- Missing short/long legs become explicit rejections.
- Stale snapshots become explicit rejections.
- Snapshot coverage is reported before P&L.
- Synthetic fallback is visible and can be blocked.

The next evidence milestone remains unchanged:

```text
Candidate B must survive conservative_mid snapshot replay with adequate
coverage before it can be considered execution-realistic.
```

## Suggested Replay Command

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --period 5y \
  --gravity-profile swing \
  --boundary-model mass_vol \
  --side-policy iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 60 \
  --min-gravity-score 50 \
  --max-levitation-score 60 \
  --min-short-hvn-distance-em 0.20 \
  --width 10 \
  --min-credit-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --use-option-snapshots data/option_chain_snapshots/QQQ \
  --entry-fill-model conservative_mid \
  --require-snapshot-fills \
  --min-snapshot-fill-coverage 80 \
  --max-snapshot-age-minutes 30 \
  --entry-timestamp-policy same_day_close \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_v05_snapshot_replay
```

## Current Status

Status: v0.5 replay guardrails are implemented. Evidence status is still
awaiting real archived option-chain coverage.

Do not promote the strategy on synthetic Candidate A/B results. Use this layer
to determine whether those results survive real option-chain pricing.
