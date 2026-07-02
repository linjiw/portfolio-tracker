# Portfolio Tracker

Open-source portfolio and trading-history analytics framework for broker CSV
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
- Market-mass / center-of-gravity boundary research and option-spread backtests.
- AI semiconductor, AI watchlist, AICS, and financial-status scoring artifacts
  that can be embedded into the dashboard when generated locally.

This is research and analytics software, not financial advice.

## Quick Start

```bash
python3 -m pip install -r requirements.txt

# Demo with synthetic sample data.
python3 generate.py \
  --input-dir examples \
  --portfolio examples/sample_portfolio_positions.csv \
  --history examples/sample_account_history.csv \
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
the input directory. It regenerates the dashboard, verifies that held-equity
market value and unrealized P&L match the broker export, then writes local
runtime output under `output/`.

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

## Core Commands

```bash
python3 sync.py [options]
  --input-dir DIR        input directory for broker CSV exports
  --portfolio PATH       explicit position snapshot CSV
  --no-fetch             reuse cached local prices
  --open                 open generated dashboard

python3 generate.py [options]
  --portfolio PATH       broker position snapshot CSV
  --history PATH         broker account activity CSV
  --input-dir DIR        input directory for auto-detection
  --out PATH             output HTML path
  --no-fetch             reuse cached local prices
  --mark-to-market       revalue held stocks from latest Yahoo prices

python3 scripts/financial_status_score.py --dashboard output/portfolio_dashboard.html
python3 scripts/market_mass_boundaries.py --price-ticker QQQ --period 5y --interval 1d --calibrate
python3 scripts/options_credit_spread_backtest.py --price-ticker QQQ --period 5y --lookback 126 --side-mode adaptive
python3 scripts/ai_watchlist_score.py
python3 scripts/aics_tool.py
```

Optional API credentials are read from environment variables or local files under
`~/.config/ptrak/`; do not commit them.

## Methodology

See [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the data contract, P&L
calculation rules, sync verification gate, financial-status scoring lens, and
market-mass research workflow.

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
