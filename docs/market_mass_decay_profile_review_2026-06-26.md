# Market Mass Decay Profile Review

Generated: 2026-06-26. Research only; not financial advice.

## Problem

The original structural center-of-mass setting used a 252-bar lookback and
63-bar half-life. For a weekly options strategy, that center can sit below any
price touched in the most recent month after a strong bull move. That is not a
math bug; it is a slow anchor. But it can be too conservative for weekly
iron-condor strike placement.

## Current NDX Centers

As of the 2026-06-26 daily bar, NDX closed at 29,118.24.

| Profile | Lookback | Half-life | Center | Distance-z | Quality | 4d 75% lower | 4d 75% upper |
|---|---:|---:|---:|---:|---:|---:|---:|
| Structural | 252 | 63 | 26,561.56 | +1.06 | 73.7 | 24,691.62 | 29,537.06 |
| Current backtest-style | 126 | 42 | 27,413.94 | +0.71 | 74.6 | 25,310.63 | 30,266.05 |
| Swing | 84 | 21 | 28,533.50 | +0.29 | 81.3 | 26,522.98 | 30,883.81 |
| Tactical | 63 | 14 | 29,189.42 | -0.05 | 84.2 | 27,692.07 | 30,745.20 |

Interpretation:

- Structural center is useful for long-horizon anchoring, but too stale for a
  one-week premium strategy after a large trend move.
- Tactical center follows the most recent month better, but can overfit local
  price action.
- Swing center is the current compromise: it raises the center materially while
  preserving enough sample depth and walk-forward behavior.

## Backtest Comparison

All runs used QQQ options, `^NDX` gravity, QQQ volume proxy, overheat blocked,
75% confidence, 10-wide spreads, and defined-risk IC simulation.

| Profile | Lookback | Half-life | Full-period trades | Win | P&L | Max DD | Walk-forward / rolling read |
|---|---:|---:|---:|---:|---:|---:|---|
| Previous broad | 126 | 42 | 32 | 84.4% | +$1,646.79 | -0.50% | Rolling: 20 trades, 85.0%, +$1,005.01, PF 2.66 |
| Tactical-fast | 63 | 14 | 75 | 84.0% | +$2,210.19 | -0.87% | Sweep selected PCS and rolling was negative; too unstable |
| Too short | 42 | 14 | 82 | 73.2% | -$244.88 | -1.41% | Overreacts to recent price action |
| Swing | 84 | 21 | 23 | 87.0% | +$1,246.62 | -0.64% | Rolling: 21 trades, 95.2%, +$1,583.25, PF 3.74 |

## Decision

Use named gravity profiles going forward:

- `structural`: 252/63 for long-horizon market structure.
- `swing`: 84/21 for weekly option strategy. This is the current preferred
  profile.
- `tactical`: 63/14 for diagnostics only unless walk-forward improves.

The algorithm should not use one center for every job. A weekly options engine
needs a faster discounted center than a long-horizon market-structure report.
That is similar to an RL discount factor: the recent state should matter more
when the action horizon is short.

## Updated Commands

Current preferred weekly IC research command:

```bash
python3 scripts/options_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --gravity-profile swing \
  --side-mode iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 45 \
  --spread-width 10 \
  --min-credit-to-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20
```

Current preferred boundary diagnostic:

```bash
python3 scripts/market_mass_boundaries.py \
  --price-ticker '^NDX' \
  --volume-ticker QQQ \
  --gravity-profile swing \
  --horizons 4,5,10,21 \
  --confidences 0.68,0.75,0.80,0.95
```
