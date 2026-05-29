# 📈 Portfolio Timeline Dashboard

Turn your Fidelity CSV exports into a single interactive HTML dashboard that
shows **every buy/sell of every stock on a price timeline**, with realized &
unrealized P&L computed against real market prices.

No server, no internet needed to view — it's one self-contained `.html` file.

![overview](docs/preview.png)

## Quick start

```bash
# 1. Export two CSVs from Fidelity and drop them in ~/Downloads:
#      Portfolio_Positions_<date>.csv   (current holdings)
#      History_for_Account_<acct>.csv   (transactions)

# 2. (first time) install the one dependency:
pip install yfinance

# 3. generate:
python3 generate.py

# 4. open it:
open output/portfolio_dashboard.html      # macOS
```

That's it. Re-run `python3 generate.py` any time you export fresh CSVs — it
auto-detects the newest files and rebuilds everything.

## Usage

```
python3 generate.py [options]

  --portfolio PATH    Portfolio Positions CSV (default: newest in --input-dir)
  --history PATH      Account History CSV       (default: newest in --input-dir)
  --input-dir DIR     where to auto-detect CSVs (default: ~/Downloads)
  --out PATH          output HTML (default: output/portfolio_dashboard.html)
  --no-fetch          reuse cached prices, no network (offline)
```

## What you get

- **KPI bar** — market value, unrealized P&L (broker-actual), realized P&L
  (window), option net cash flow, net invested, deposits.
- **Searchable / sortable stock list** — held / exited / all.
- **Per-stock timeline chart** (hand-drawn SVG):
  - gray = real market price (Yahoo Finance)
  - orange dashed = your running average cost
  - blue dashed = current price
  - green/red dots = buys/sells, **dot size ∝ trade amount**, hover for details
- **Transaction table** — price, amount, position after, avg cost after,
  realized P&L per sell.
- **Options section** — net cash flow per contract.
- **Fibonacci momentum** — per stock: EMA 5/8/13/21 ribbon with state coloring
  (range / trend / transition), momentum oscillator, RSI(14), golden/death
  crosses; plus a momentum ranking across holdings on the overview. (Technical
  reference, not investment advice — Fib periods aren't magic, the value is the
  geometric fast/mid/slow layering.)

## How numbers are computed

- **Unrealized P&L**: taken directly from the Portfolio CSV (broker-exact).
- **Realized P&L**: average-cost method over the transaction window. Stocks held
  before the history window started are flagged "含旧仓" and their legacy cost
  is *estimated* at the market price on the window's first day — so those
  realized figures are approximate.

See **[SKILLS.md](SKILLS.md)** for the full methodology, data conventions, and
gotchas (start there if extending or debugging).

## Project layout

```
portfolio-tracker/
├── generate.py     # the whole pipeline (parse → fetch prices → build HTML)
├── README.md       # this file
├── SKILLS.md       # methodology + maintenance notes
└── output/
    ├── portfolio_dashboard.html   # the generated dashboard
    └── prices_cache.json          # cached Yahoo prices (for --no-fetch)
```

## Requirements

- Python 3
- [`yfinance`](https://pypi.org/project/yfinance/) (only needed when generating;
  the output HTML needs nothing).
