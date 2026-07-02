# Market Mass Reference Optimization

Generated: 2026-06-26. Research only; not financial advice.

## Reference Inputs Used

Reviewed:

- `market_gravity_tool_bundle/market_gravity_tool.py`
- `market_gravity_tool_bundle/market_gravity_README.md`
- local pasted research note

Useful ideas merged into the repo:

- Absorption-adjusted mass: volume in tight ranges carries more mass than
  volume in wide momentum bars.
- OU gravity diagnostics: `kappa`, `kappa_tstat`, OU half-life, residual sigma.
- Amihud-style thinness / lightness score.
- `gravity_score` and `levitation_score`.
- Volume-profile high-volume node support/resistance diagnostics.
- Optional `--min-gravity-score` and `--max-levitation-score` trade filters.
- Explicit `--boundary-model` switch:
  - `mass_vol`: default, proven mass/profile plus volatility blend.
  - `ou_hybrid`: research mode; OU can influence final boundaries.

## Important Finding

Do not let OU boundaries control strikes by default yet.

The first direct test with OU influencing the final boundary (`ou_hybrid`)
looked worse:

| Model | Gravity filter | Trades | Win | P&L | Max DD | Profit factor | Touch |
|---|---|---:|---:|---:|---:|---:|---:|
| `ou_hybrid` | none | 46 | 76.1% | +$54.33 | -1.59% | 1.01 | 47.8% |

Interpretation: OU mean reversion is useful as a diagnostic, but forcing it
into final weekly IC strikes made the structure too permissive / too noisy.
The reference layer should first act as a regime filter, not as the main strike
engine.

## Corrected Experiment

Using the default `mass_vol` final boundary model with the reference diagnostics
available:

| Filters | Trades | Win | P&L | Max DD | Profit factor | Touch | Avg gravity | Avg levitation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| none | 31 | 83.9% | +$1,011.32 | -0.66% | 1.60 | 32.3% | 67.7 | 35.4 |
| gravity >= 60, levitation <= 55 | 25 | 88.0% | +$1,254.74 | -0.65% | 2.14 | 32.0% | 71.3 | 31.2 |
| gravity >= 55, levitation <= 65 | 27 | 88.9% | +$1,453.83 | -0.65% | 2.32 | 29.6% | 70.4 | 32.7 |
| gravity >= 65, levitation <= 50 | 19 | 89.5% | +$1,179.90 | -0.65% | 2.62 | 31.6% | 73.9 | 28.2 |
| gravity >= 50, levitation <= 60 | 28 | 89.3% | +$1,533.02 | -0.65% | 2.40 | 28.6% | 69.8 | 33.4 |

Best nearby direct setting:

```bash
--boundary-model mass_vol \
--min-gravity-score 50 \
--max-levitation-score 60
```

## Bounded Sweep Result

Fixed:

- QQQ options.
- `^NDX` gravity.
- QQQ volume proxy.
- `--gravity-profile swing`.
- `--boundary-model mass_vol`.
- `--min-gravity-score 50`.
- `--max-levitation-score 60`.
- `overheat` blocked.

Best walk-forward candidate:

| Policy | Confidence | Quality min | Build max | Width | Trades | Win | Return | P&L | PF | Touch |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| iron_condor | 0.75 | 55 | 60 | 10 | 31 | 90.3% | 1.41% | +$1,410.92 | 2.20 | 29.0% |

Rolling calendar walk-forward for the same family:

- Windows: 3
- Trades: 31
- Win rate: 90.3%
- P&L: +$1,406.79
- Profit factor: 2.19
- Touch rate: 29.0%
- Worst max drawdown: -0.66%

## Current Diagnostic Snapshot

Fresh rerun returned a latest yfinance daily bar of 2026-06-25. Keep this
timestamp explicit; do not mix it with earlier 2026-06-26 snapshots.

NDX with QQQ volume proxy, `swing` profile:

- Price: 29,440.32
- Center: 28,477.02
- Quality: 78.8
- Distance-z: +0.45
- Regime: `active_center`
- Gravity score: 69.6
- Levitation score: 30.4
- Gravity regime: `centered_mean_reverting`
- OU kappa / t-stat: 0.1666 / 2.65
- OU half-life: 4.16 bars
- Build-up: 16.1
- Nearest support HVN: 29,304.53
- Nearest resistance HVN: 29,481.81

QQQ direct, `swing` profile:

- Price: 716.38
- Center: 693.24
- Quality: 78.7
- Distance-z: +0.45
- Gravity score: 69.4
- Levitation score: 30.6
- Nearest support HVN: 713.10
- Nearest resistance HVN: 743.91

## Updated Recommendation For The Engine

Use this as the next weekly IC research default:

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

## Next Improvements

- Add a grid over `min_gravity_score` and `max_levitation_score` instead of
  manually sweeping nearby values.
- Track losing trades by gravity bucket, levitation bucket, and HVN distance.
- Test whether HVN proximity should require wider strikes when a short strike
  sits just inside a high-volume node.
- Keep collecting real option-chain snapshots; synthetic option pricing remains
  the largest model risk.
