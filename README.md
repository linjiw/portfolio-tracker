# Portfolio Tracker

Local-first portfolio and trading-history analytics framework for broker CSV
exports. It turns positions, activity, prices, and optional research artifacts
into a self-contained HTML dashboard for portfolio review, trading behavior
analysis, and market-structure research.

The repository intentionally does **not** include personal portfolio exports,
generated dashboards, logs, API caches, account identifiers, or trading history.
Those files are local runtime data and are ignored by Git.

## What It Builds

- Portfolio timeline dashboard from broker position and activity CSVs.
- Realized/unrealized P&L views, cost-basis path, trade markers, and option cash
  flow summaries.
- Risk, concentration, behavior, journal, and trend-execution views.
- QQQ/TQQQ market-sentinel and intraday tape workflows.
- Rolling 1/2/3-month close-versus-open, range-midpoint, VWAP-proxy, and
  closing-bucket diagnostics for the current portfolio and QQQ.
- Market-mass / center-of-gravity boundary research and option-spread backtests.
- AI semiconductor, AI watchlist, AICS, and financial-status scoring artifacts
  that can be embedded into the dashboard when generated locally.
- Korean/U.S. memory-market flow monitor with KOFIA market-level margin/credit
  balances, official KRX short-volume evidence, KRX-derived secondary investor
  categories, U.S. retail/short-volume proxies, ADR parity, explicit hypothesis
  falsification, and a research-only dealer-scenario lens. Missing
  stock-specific Korean margin balances and signed dealer inventory remain
  blocking evidence gaps, never inferred from public proxies.

This is research and analytics software, not financial advice.

## Quick Start

```bash
python3 -m pip install -r requirements.txt

# Demo with synthetic sample data.
python3 generate.py \
  --input-dir examples \
  --portfolio examples/sample_portfolio_positions.csv \
  --history examples/sample_account_history.csv \
  --artifact-dir examples \
  --out output/demo_dashboard.html

open output/demo_dashboard.html
```

For real local use, export your broker files into a private local directory and
run:

```bash
python3 sync.py --input-dir ~/Downloads --open
```

By default, `sync.py` detects the newest `Portfolio_Positions*.csv` and merges
all available `Accounts_History*.csv` / `History_for_Account*.csv` exports in
the input directory. It archives imported sources by content hash, regenerates
the dashboard, and independently verifies equities, cash, pending activity,
option marks/P&L, option-leg count, and whole-account arithmetic before the
snapshot is accepted.

For the complete close workflow (broker gate, latest prices, every daily
analysis producer, final render, and downstream risk reports), run the locked
orchestrator with exact new exports:

```bash
python3 scripts/refresh_portfolio_intelligence.py \
  --portfolio ~/Downloads/Portfolio_Positions_Jul-09-2026.csv \
  --history ~/Downloads/Accounts_History.csv \
  --as-of 2026-07-09
```

Each run writes an immutable manifest under `output/refresh_runs/` and updates
`output/latest_refresh_manifest.json`. Long backtests and the intraday tape are
intentionally outside this close workflow.

Intraday QQQ workflows are fail-closed decision support, not an execution
engine. They require a timezone-aware, validity-bounded
`output/intraday_tape/events.json`; missing/expired event coverage produces
`BLOCK_DATA`. Closed-bar freshness, session holidays/early closes, same-time
volume denominators, option-expiry calendar coverage, and Judge output/gate
agreement are enforced deterministically. See [AUTOMATION.md](AUTOMATION.md)
for the runtime contract. Option structures remain WATCH candidates until a
current chain supplies liquidity, debit/credit, and max-loss economics.

## Privacy Model

Private runtime files are ignored:

- broker CSV exports: `Portfolio_Positions*.csv`, `Accounts_History*.csv`,
  `History_for_Account*.csv`
- generated dashboard/data: `output/`
- local API caches, logs, screenshots, PID files, and credentials
- local agent/editor config such as `.claude/`

Before publishing or contributing, run:

```bash
git status --short
git ls-files output
git grep -n -I -E 'account|netWorth|lifeDeposits|Portfolio_Positions|Accounts_History|History_for_Account|apiKey|token|chat_id'
```

`git ls-files output` should print nothing for the public repo. Keep real
account exports and generated dashboards outside Git.

Current-tree checks are not sufficient after an accidental commit: old Git
objects can remain publicly addressable. See [SECURITY.md](SECURITY.md) for the
confirmed historical-artifact warning and the coordinated remediation plan.

## Core Commands

```bash
python3 sync.py [options]
  --input-dir DIR        input directory for broker CSV exports
  --portfolio PATH       explicit position snapshot CSV
  --history PATH         newest activity export (cumulative by default)
  --history-mode MODE    cumulative or exact
  --no-fetch             reuse cached local prices
  --open                 open generated dashboard

python3 scripts/refresh_portfolio_intelligence.py [options]
  --portfolio PATH       exact broker position snapshot (or auto-detect newest)
  --history PATH         newest activity export (or auto-detect newest)
  --as-of YYYY-MM-DD     required market-close date for freshness checks
  --no-fetch             cache-only run; live-only producers are skipped visibly

python3 generate.py [options]
  --portfolio PATH       broker position snapshot CSV
  --history PATH         broker account activity CSV
  --history-start DATE   explicit analysis start; default earliest known trade
  --input-dir DIR        input directory for auto-detection
  --artifact-dir DIR     optional research JSON directory; defaults to output directory
  --out PATH             output HTML path
  --no-fetch             reuse cached local prices
  --mark-to-market       revalue held stocks from latest Yahoo prices

python3 scripts/financial_status_score.py --dashboard output/portfolio_dashboard.html
  # Historical --as-of is rejected unless --allow-non-point-in-time is explicit.

python3 scripts/memory_flow.py
  # Writes output/memory_flow.json + memory_flow_report.md and refreshes the
  # private cache. Add --no-fetch to rebuild from the last cache.
python3 scripts/spmo_momentum_sleeve.py
  # For a known close/re-entry between snapshots: --reset-stop --reset-reason "..."
python3 scripts/momentum_overlay_research.py
  # Ten-year SPY/QQQ/SPMO/11M-Top3 overlay research with chronological folds;
  # 2024+ is now a previously exposed fixed replay, not an untouched holdout.
  # Add --no-fetch to reproduce from the private market and Top-3 caches.
python3 scripts/momentum_policy_lab.py --no-fetch
  # One parameter set across SPY/QQQ and five independent momentum ETFs,
  # including annual nested pseudo-OOS selection and a fixed 2024+ replay.
python3 scripts/momentum_final_candidate.py
  # Focused 6/10/12-month asymmetric ON/OFF calibration with cost/funding stress.
python3 scripts/momentum_rl_research.py --epochs 1
  # Slow research-only tabular challenger. The default 15 epochs takes much
  # longer; historical success can qualify only for prospective paper testing.
python3 scripts/close_vs_intraday.py --dashboard output/portfolio_dashboard.html
python3 scripts/market_mass_boundaries.py --price-ticker QQQ --period 5y --interval 1d --calibrate
python3 scripts/options_credit_spread_backtest.py --price-ticker QQQ --period 5y --lookback 126 --side-mode adaptive
  # Daily/point-in-time research only: prior-bar signals, Friday expiry mapping,
  # non-overlapping capital, explicit dividend/IV/slippage assumptions, nested walk-forward.
python3 scripts/ai_watchlist_score.py
python3 scripts/aics_tool.py
```

AI score outputs distinguish curated priors from observed market data. Non-USD
technical returns are converted to USD with historical FX, unsourced/expired
watchlist evidence is numerically capped, and quarantined rows are not assigned
comparative final-score percentiles. AICS capital-flow/valuation fields are
explicit proxies; its current-rank baskets are descriptive rather than a
backtest, and saved-snapshot validation uses a one-snapshot execution lag while
remaining non-decision-grade until corporate-action/dividend continuity and
execution data are independently reconciled.

Optional API credentials are read from environment variables or local files under
`~/.config/ptrak/`; do not commit them.

## Methodology

See [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the data contract, P&L
calculation rules, sync verification gate, financial-status scoring lens, and
market-mass research workflow.

See [docs/MOMENTUM_RESEARCH_CONCLUSION.md](docs/MOMENTUM_RESEARCH_CONCLUSION.md)
for the corrected ten-year momentum-overlay result, cross-asset falsification,
SPMO shadow challenger, and conservative RL disposition.

See [README_options_credit_spread_backtest.md](README_options_credit_spread_backtest.md)
for the option-spread research model.

## Project Layout

```text
portfolio-tracker/
├── generate.py                 # CSV parsing, price fetch, dashboard renderer
├── sync.py                     # one-command sync + verification gate
├── scripts/                    # scoring, automation, market research tools
├── tests/                      # regression tests
├── docs/                       # methodology and research notes
├── examples/                   # synthetic sample CSVs
└── output/                     # local generated artifacts, ignored by Git
```

## Requirements

- Python 3.10+
- `yfinance`, `pandas`, `numpy`
- `pytest` for tests

```bash
python3 -m pytest -q
```
