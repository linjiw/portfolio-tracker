# Market Mass v0.3 Robustness Review

Generated: 2026-06-26. Research only; not financial advice.

## Objective

Continue from the reference-filtered baseline and make the model harder to
fool. The reviewed report recommended three checks before increasing model
complexity:

- parameter plateau over `gravity_score` / `levitation_score`;
- losing-trade decomposition;
- robustness tests for one-bar signal lag and harsher option fills.

## Implemented

Code changes:

- Added `--signal-lag-bars` to force signals to come from prior completed bars.
- Added `--credit-haircut-pct` to reduce modeled entry credit after slippage.
- Added HVN strike-distance diagnostics and optional
  `--min-short-hvn-distance-em`.
- Added sweep dimensions:
  - `--grid-min-gravity-scores`
  - `--grid-max-levitation-scores`
- Added loss outputs:
  - `loss_analysis.json`
  - `losing_trades.csv`

## Current Research Champion

Command family:

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --interval 1d \
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
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20
```

Direct result:

| Trades | Win | P&L | Max DD | PF | Touch | Avg short-HVN distance |
|---:|---:|---:|---:|---:|---:|---:|
| 28 | 89.3% | +$1,533.02 | -0.65% | 2.40 | 28.6% | 0.35 EM |

Loss decomposition:

| Cause | Count |
|---|---:|
| HVN magnet risk | 3 |
| Short strike touched | 3 |
| Stop loss | 2 |
| Large expiry move | 1 |

Interpretation:

- The losses are not random. All three losing trades were short-strike touches
  near high-volume nodes.
- That supports the report's warning that an HVN can be a magnet, not only
  support/resistance.

## Robustness Tests

| Variant | Trades | Win | P&L | Max DD | PF | Touch | Read |
|---|---:|---:|---:|---:|---:|---:|---|
| Current champion | 28 | 89.3% | +$1,533.02 | -0.65% | 2.40 | 28.6% | Strong direct result |
| Signal lag 1 bar | 26 | 76.9% | +$528.38 | -0.70% | 1.29 | 46.2% | Edge weakens materially |
| Credit haircut 20% | 21 | 61.9% | -$671.87 | -0.86% | 0.65 | 42.9% | Fails fill-sensitivity test |

Interpretation:

- The current research edge is timing-sensitive.
- The synthetic option pricing layer is still the largest model risk.
- A real option-chain snapshot archive is required before promoting this beyond
  research/paper-trading confidence.

## HVN Filter Tests

| Min short-HVN distance | Trades | Win | P&L | Max DD | PF | Touch | Read |
|---:|---:|---:|---:|---:|---:|---:|---|
| 0.00 EM | 28 | 89.3% | +$1,533.02 | -0.65% | 2.40 | 28.6% | Current champion |
| 0.20 EM | 20 | 95.0% | +$1,364.84 | -0.65% | 3.06 | 20.0% | Defensive variant |
| 0.30 EM | 17 | 94.1% | +$1,018.84 | -0.65% | 2.54 | 17.6% | Too much pruning |
| 0.40 EM | 10 | 90.0% | +$330.86 | -0.66% | 1.50 | 30.0% | Too few trades |
| 0.75 EM | 1 | 100.0% | +$138.32 | 0.00% | 999.0 | 0.0% | Unusable over-prune |

Interpretation:

- `--min-short-hvn-distance-em 0.20` is promising as a defensive variant.
- It improves win rate, PF, and touch rate, but sacrifices trade count and total
  P&L.
- Do not make this the default yet; keep it as a risk-reduction mode.

## Parameter Plateau

Bounded grid:

```text
min_gravity_score:     45, 50, 55, 60, 65
max_levitation_score:  45, 50, 55, 60, 65, 70
```

With other IC settings fixed, the sweep showed a stronger-ranked CCS-only
cluster:

- Best walk-forward family: `ccs_only`, confidence 0.75, width 10,
  gravity >= 65.
- Rolling result: 25 trades, 84.0% win, +$1,433.85, PF 2.32, touch 40.0%,
  worst drawdown about -0.50%.

Interpretation:

- This does not replace the IC research champion yet.
- It indicates that in this sample, upper call-side risk control has been a
  stable source of modeled returns.
- The CCS-only cluster should be tested separately with position-context rules,
  because a call credit spread is directional short premium unless paired with
  long exposure.

## Updated Status

Status: current research champion, not production/live confidence.

Current default remains:

```text
mass_vol boundary
swing gravity profile
gravity >= 50
levitation <= 60
iron condor, 75% confidence, 10-wide
```

Defensive variant:

```text
add --min-short-hvn-distance-em 0.20
```

Primary blockers before confidence upgrade:

- Results weaken under one-bar signal lag.
- Results fail under a 20% credit haircut.
- Historical option fills are synthetic.

Next step:

Build the option-chain snapshot archive and compare modeled credit versus
actual available bid/ask credit for the selected strikes.
