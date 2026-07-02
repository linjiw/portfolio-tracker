# Market Mass v0.4 Snapshot-Replay Foundation

Generated: 2026-06-27. Research only; not financial advice.

## Objective

Continue from the v0.3 robustness pass without adding another loose parameter.
The blocking issue is execution realism: synthetic option credit can make a
weekly short-premium strategy look much better than real bid/ask fills.

This pass adds the foundation for point-in-time option-chain archiving and
snapshot-based entry-credit replay.

## Implemented

Code changes:

- Added `scripts/archive_option_chain_snapshots.py`.
- Added archived option-chain replay support to
  `scripts/market_mass_credit_spread_backtest.py`.
- Added snapshot fill models:
  - `mid`
  - `natural`
  - `conservative_mid`
  - `haircut_mid`
  - `bid_ask_slippage`
- Added per-trade credit diagnostics:
  - `synthetic_credit_per_share`
  - `entry_credit_source`
  - `snapshot_mid_credit`
  - `snapshot_natural_credit`
  - `snapshot_conservative_credit`
  - `snapshot_fill_credit`
  - `credit_error_pct`
- Added HVN outcome diagnostics:
  - `hvn_outcome_analysis.csv`
  - `hvn_outcome_analysis.json`
- Added a mandatory "Why This Is Not Live-Ready" section to generated reports.

## Snapshot Archive

Dry-run smoke test:

```bash
python3 scripts/archive_option_chain_snapshots.py \
  --ticker QQQ \
  --max-expiries 1 \
  --skip-gravity-state \
  --dry-run
```

Result:

```text
Archived 183 option rows for QQQ at 2026-06-27T03:01:39-04:00
dry-run:data/option_chain_snapshots/QQQ/2026/06/2026-06-27_030139_ET_QQQ_2026-06-29.csv
```

The dry run confirmed yfinance option-chain access and partitioned output path
generation. It did not write files.

Gravity-enabled dry-run also succeeded:

```text
Archived 183 option rows for QQQ at 2026-06-27T03:02:43-04:00
dry-run:data/option_chain_snapshots/QQQ/2026/06/2026-06-27_030243_ET_QQQ_2026-06-29.csv
```

## Candidate A: Research Champion

Output:

```text
output/options_backtest/qqq_ndx_mass_ic_v04_candidate_a/
```

| Trades | Win | P&L | Max DD | PF | Touch | Avg HVN distance |
|---:|---:|---:|---:|---:|---:|---:|
| 28 | 89.3% | +$1,533.02 | -0.65% | 2.40 | 28.6% | 0.35 EM |

Loss decomposition:

| Cause | Count |
|---|---:|
| HVN magnet risk | 3 |
| Short strike touched | 3 |
| Stop loss | 2 |
| Large expiry move | 1 |

HVN outcome read:

| Group | Trades | Avg EM | Median EM | <0.20 EM | <0.75 EM |
|---|---:|---:|---:|---:|---:|
| Winners | 25 | 0.36 | 0.35 | 24.0% | 96.0% |
| Losers | 3 | 0.26 | 0.19 | 66.7% | 100.0% |
| Touched winners | 5 | 0.33 | 0.28 | 40.0% | 100.0% |
| Touched losers | 3 | 0.26 | 0.19 | 66.7% | 100.0% |

Interpretation:

- HVN proximity still explains losses, but the broad `<0.75 EM` threshold is
  not selective because most winners also sit inside it.
- The sharper `<0.20 EM` threshold is more informative: 66.7% of losers versus
  24.0% of winners.
- This supports using `0.20 EM` as a defensive filter, but it is still a small
  loser sample.

## Candidate B: Defensive HVN Variant

Output:

```text
output/options_backtest/qqq_ndx_mass_ic_v04_candidate_b_hvn020/
```

| Trades | Win | P&L | Max DD | PF | Touch | Avg HVN distance |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 95.0% | +$1,364.84 | -0.65% | 3.06 | 20.0% | 0.43 EM |

Loss decomposition:

| Cause | Count |
|---|---:|
| HVN magnet risk | 1 |
| Short strike touched | 1 |
| Stop loss | 1 |

HVN outcome read:

| Group | Trades | Avg EM | Median EM | <0.20 EM | <0.75 EM |
|---|---:|---:|---:|---:|---:|
| Winners | 19 | 0.43 | 0.37 | 0.0% | 94.7% |
| Losers | 1 | 0.40 | 0.40 | 0.0% | 100.0% |
| Touched winners | 3 | 0.45 | 0.53 | 0.0% | 100.0% |
| Touched losers | 1 | 0.40 | 0.40 | 0.0% | 100.0% |

Interpretation:

- The `0.20 EM` filter removes the closest HVN-magnet cases.
- It improves win rate, profit factor, and touch rate, but total P&L drops
  because it skips eight trades.
- It should remain the risk-aware research candidate, not a proven live rule.

## Snapshot Replay Usage

Archive:

```bash
python3 scripts/archive_option_chain_snapshots.py \
  --ticker QQQ \
  --max-expiries 2 \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --gravity-profile swing \
  --boundary-confidence 0.75 \
  --out-dir data/option_chain_snapshots
```

Replay:

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
  --width 10 \
  --min-credit-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --use-option-snapshots data/option_chain_snapshots/QQQ \
  --entry-fill-model conservative_mid \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_snapshot_replay
```

## Current Status

Status: v0.4 execution-realism foundation is implemented, but the strategy is
still not live-ready.

Reasons:

- Existing historical results are still mostly synthetic because we do not yet
  have a historical archive of point-in-time option chains.
- Snapshot replay currently replaces entry credit only; exit marks remain
  modeled from the underlying path.
- v0.3 showed material weakness under one-bar signal lag.
- v0.3 failed under a 20% synthetic credit haircut.
- Candidate B looks better on risk metrics, but the loser sample remains small.

Next step:

Start saving option-chain snapshots during live market sessions and compare
synthetic credit against `conservative_mid` and `natural` archived credits for
every selected weekly structure.
