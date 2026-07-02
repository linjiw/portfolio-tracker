# Options Credit Spread Backtest

Research/model backtester for weekly defined-risk QQQ credit spreads using
market-mass gravity signals. It is not a live trade signal and does not use
historical option-chain fills. Premium is estimated from Black-Scholes with
entry-date volatility context, then reduced by slippage and commissions.

## Core Commands

Trade QQQ spreads using QQQ mass:

```bash
python3 scripts/options_credit_spread_backtest.py \
  --price-ticker QQQ \
  --period 5y \
  --lookback 126 \
  --half-life 42 \
  --side-mode adaptive \
  --confidence 0.80 \
  --spread-width 5 \
  --min-center-quality 60 \
  --max-build-up 60 \
  --min-credit-to-risk 0.10 \
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20 \
  --out-dir output/options_backtest/qqq
```

Trade QQQ spreads using NDX gravity and QQQ volume proxy:

```bash
python3 scripts/options_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --lookback 126 \
  --half-life 42 \
  --side-mode adaptive \
  --confidence 0.80 \
  --spread-width 5 \
  --out-dir output/options_backtest/qqq_ndx_mass
```

Run bounded grid search plus rolling calendar walk-forward:

```bash
python3 scripts/options_credit_spread_backtest.py \
  --price-ticker QQQ \
  --period 5y \
  --lookback 126 \
  --side-mode adaptive \
  --grid-search \
  --grid-confidences 0.68,0.80,0.90 \
  --grid-widths 3,5,10 \
  --grid-min-qualities 55,65,75 \
  --grid-max-builds 45,60,72 \
  --grid-credit-to-risks 0.08,0.10,0.15 \
  --max-short-delta 0.35 \
  --sweep-limit 120 \
  --out-dir output/options_backtest/qqq_grid
```

Current NDX-gravity iron-condor research candidate:

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --gravity-profile swing \
  --boundary-model mass_vol \
  --side-mode iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 60 \
  --min-gravity-score 50 \
  --max-levitation-score 60 \
  --spread-width 10 \
  --min-credit-to-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20 \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_current
```

Defensive HVN variant:

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --gravity-profile swing \
  --boundary-model mass_vol \
  --side-mode iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 60 \
  --min-gravity-score 50 \
  --max-levitation-score 60 \
  --min-short-hvn-distance-em 0.20 \
  --spread-width 10 \
  --min-credit-to-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20 \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_defensive_hvn
```

Candidate A is the raw research champion. Candidate B is the same model with
`--min-short-hvn-distance-em 0.20`; treat it as the risk-aware candidate until
more trades prove whether HVN magnet risk separates losers from non-losers.

Gravity profiles:

- `--gravity-profile structural`: 252-bar lookback, 63-bar half-life. Best for long-horizon market structure, but can lag after a fast bull move.
- `--gravity-profile swing`: 84-bar lookback, 21-bar half-life. Current preferred weekly-options profile because it keeps enough sample size while adapting to recent accepted participation.
- `--gravity-profile tactical`: 63-bar lookback, 14-bar half-life. Faster center, useful for diagnostics, but more prone to overreacting.

Reference-derived gravity diagnostics:

- Mass now includes an absorption term: the same dollar volume gets more mass when it trades in a tighter range, and less mass when it appears in a wide momentum bar.
- The boundary report estimates OU-style mean reversion around the rolling center: `kappa`, `kappa_tstat`, OU half-life, and OU residual sigma.
- `gravity_score` asks whether the center has pull; `levitation_score` asks whether price is moving through thin or momentum-dominated space.
- Volume-profile high-volume nodes are reported as nearby support/resistance candidates. They are structural nodes, not guaranteed reversal levels.

Strict two-wing income-condor variant:

```bash
python3 scripts/options_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --period 5y \
  --gravity-profile swing \
  --side-mode iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 45 \
  --spread-width 10 \
  --min-credit-to-risk 0.08 \
  --min-ic-wing-credit 0.03 \
  --blocked-ic-weather overheat \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_true_condor
```

## Strategy Filters

- `--min-center-quality`: skip weak/diffuse mass states.
- `--max-build-up`: skip high stored-energy regimes.
- `--max-abs-distance-z`: skip price too far from mass center.
- `--min-gravity-score`: optional filter requiring enough OU/center/vol/thickness support before entry.
- `--max-levitation-score`: optional filter blocking thin or momentum-dominated states.
- `--min-short-hvn-distance-em`: optional HVN magnet filter. Rejects short strikes too close to a high-volume node, measured in expected-move units.
- `--signal-lag-bars`: robustness test that forces signals to come from prior completed bars.
- `--credit-haircut-pct`: fill-sensitivity test that reduces modeled entry credit after slippage.
- `--use-option-snapshots`: replay entry credits from archived point-in-time option-chain CSVs.
- `--entry-fill-model`: with snapshots, choose `mid`, `natural`, `conservative_mid`, `haircut_mid`, or `bid_ask_slippage`.
- `--require-snapshot-fills` / `--no-synthetic-fallback`: fail closed in snapshot replay; do not silently use synthetic entry credits.
- `--min-snapshot-fill-coverage`: report whether replay coverage clears this required percentage of attempted structures.
- `--max-snapshot-age-minutes`: reject stale option-chain snapshots relative to the entry timestamp policy.
- `--entry-timestamp-policy`: choose `same_day_close`, `same_day_open`, or `next_open` when matching daily signals to archived snapshots.
- `--boundary-model mass_vol`: default. Uses the proven mass/profile plus realized/implied-volatility blend for final strike boundaries.
- `--boundary-model ou_hybrid`: research mode. Lets OU mean-reversion influence final profile boundaries after it is measured.
- `--boundary-buffer-pct`: pushes short strikes farther beyond the model boundary.
- `--max-short-delta`: blocks short legs that are too close to spot.
- `--max-quality-drop-5d`: skips entries when center quality is degrading too fast.
- `--max-abs-distance-z-slope-5d`: skips entries when price is moving away from/toward mass too fast.
- `--blocked-ic-weather`: blocks IC entries in selected QQQ weather labels, such as `overheat`.
- `--min-ic-body-pct`: requires a minimum distance between IC short strikes as a percentage of spot.
- `--min-ic-wing-credit`: requires each IC wing to collect at least this modeled credit.
- `--min-ic-credit-balance`: requires the smaller wing credit to be at least this fraction of the larger wing credit.
- `--min-credit` and `--min-credit-to-risk`: reject weak reward/risk spreads.
- `--breach-stop`: optional exit when the short strike is touched before expiry.

Iron condors are modeled as one combined structure:

- one put credit spread plus one call credit spread,
- one net credit,
- one structure-level max loss,
- one profit target / stop path,
- and one short-touch flag if either short leg is touched.

This avoids overstating risk by treating both sides as unrelated trades, while
still charging four-leg commissions.

The model distinguishes an asymmetric gravity condor from a strict income
condor. If `--min-ic-wing-credit` is zero, one wing may contribute little or no
premium; that behaves more like a directional gravity spread with defined risk
on both sides. Use a positive `--min-ic-wing-credit` or
`--min-ic-credit-balance` when you want a true two-wing premium structure.

## Outputs

Additional research diagnostics:

- `loss_analysis.json`: losing-trade bucket counts and per-loss explanations.
- `losing_trades.csv`: each losing trade with gravity, levitation, build-up, touch side, HVN distance, and cause tags.
- `hvn_outcome_analysis.csv/json`: HVN distance by winners, losers, touched winners, and touched losers.
- `snapshot_replay_rejections.csv/json`: structures rejected by real-chain replay because of missing/stale quotes, missing legs, low credit, low credit/risk, or other execution filters.

- `summary.json`: full assumptions, baseline, trades, grid, and rolling results.
- `trades.csv`: modeled spread entries/exits, strikes, credit, delta, P&L, mass state.
- `skipped.csv`: skipped weekly candidates and reasons.
- `sweep_train.csv`: top in-sample grid candidates.
- `sweep_walk_forward.csv`: candidates tested on the later holdout slice.
- `rolling_walk_forward.csv`: rolling calendar-year validation.
- `rolling_walk_forward_windows.csv`: per-window rolling details.
- `report.md`: compact human-readable report.

## Interpretation

Do not optimize for win rate alone. A useful candidate should keep positive
expectancy, profit factor above 1, controlled drawdown, acceptable touch rate,
and positive P&L per dollar of risk across holdout and rolling windows.

Free data is enough to test whether gravity boundaries improve filtering and
strike placement. It is not enough to prove historical option fill quality.
The next validation layer should archive live option-chain snapshots and paper
trade the model before any real-money use.

## Option-Chain Snapshot Archive

Archive QQQ chains with NDX gravity context:

```bash
python3 scripts/archive_option_chain_snapshots.py \
  --ticker QQQ \
  --max-expiries 2 \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --gravity-profile swing \
  --boundary-confidence 0.75 \
  --out-dir data/option_chain_snapshots
```

Rows are written under:

```text
data/option_chain_snapshots/QQQ/YYYY/MM/YYYY-MM-DD_HHMMSS_ET_QQQ_EXPIRY.csv
data/option_chain_snapshots/QQQ/raw/YYYY-MM-DD/YYYY-MM-DD_HHMMSS_ET_QQQ_EXPIRY_raw.json
data/option_chain_snapshots/QQQ/metadata/YYYY-MM-DD/YYYY-MM-DD_HHMMSS_ET_QQQ_EXPIRY_metadata.json
```

Each row stores bid/ask/mid, volume/open interest, IV, approximate Greeks, and
the same-timestamp gravity fields: center, quality, build-up, gravity score,
levitation score, boundary lower/upper, and HVN distance for that strike.
Raw yfinance rows and metadata are preserved by default so normalized snapshots
can be rebuilt later if parsing, timezone, or Greek assumptions change.

Replay backtests using archived entry credits:

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --period 5y \
  --gravity-profile swing \
  --boundary-model mass_vol \
  --side-mode iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 60 \
  --min-gravity-score 50 \
  --max-levitation-score 60 \
  --spread-width 10 \
  --min-credit-to-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --use-option-snapshots data/option_chain_snapshots/QQQ \
  --entry-fill-model conservative_mid \
  --require-snapshot-fills \
  --min-snapshot-fill-coverage 80 \
  --max-snapshot-age-minutes 30 \
  --entry-timestamp-policy same_day_close \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_snapshot_replay
```

Snapshot replay currently replaces entry credit only. Exit marks are still
modeled from the underlying path, so this is a v0.4 bridge toward real fill
validation, not complete historical option-chain reconstruction.

Coverage fields in `summary.json` and `report.md` should be read before the
headline P&L:

- `pricing_source`
- `snapshot_fill_coverage_pct`
- `trades_with_snapshot_entry`
- `trades_falling_back_to_synthetic`
- `snapshot_rejection_count`
- `avg_snapshot_age_minutes`
- `max_snapshot_age_minutes`

If coverage is low, the replay result is an execution-data finding, not a
strategy-performance result.
