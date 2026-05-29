# SKILLS.md — Portfolio Timeline Dashboard

> Methodology, conventions and gotchas for regenerating the dashboard when the
> CSVs change. Read this first if you (or a future Claude session) need to
> rebuild, extend, or debug the tool.

## What this project does

Turns two Fidelity CSV exports into one self-contained interactive HTML page
that visualizes **every buy/sell of every stock on a price timeline**, plus
realized & unrealized P&L. Pure HTML + SVG + vanilla JS — no server, no build
step, no external libraries at view time. Just open the `.html` file.

## Inputs (the two CSVs)

1. **Portfolio Positions CSV** — `Portfolio_Positions_*.csv`
   - Current holdings snapshot. Columns used: `Symbol`(2), `Quantity`(4),
     `Last Price`(5), `Current Value`(7), `Total Gain/Loss Dollar`(10),
     `Cost Basis Total`(13).
   - Rows to skip: `SPAXX**` (money market), `Pending activity`, blank symbols,
     and option rows (symbol starts with `-`).
   - A symbol can appear in **multiple lots** (Cash + Margin) — sum them.
   - This file is the **source of truth for current holdings, average cost,
     and unrealized P&L** (broker-accurate).

2. **Account History CSV** — `History_for_Account_*.csv`
   - Transaction log. Columns used: `Run Date`(0), `Action`(1), `Symbol`(2),
     `Description`(3), `Price`(5), `Quantity`(6), `Amount`(10).
   - Real header starts a few rows down; filter rows where col 0 matches
     `MM/DD/YYYY`.
   - `Action` text drives side: contains `BOUGHT` → BUY, `SOLD` → SELL.
   - `Quantity` is unsigned in some rows / signed in others → always use `abs()`
     and rely on the BOUGHT/SOLD text for direction.
   - `Amount` sign: negative = cash out (buy), positive = cash in (sell).
   - Skip `Electronic Funds Transfer` and `TRANSFERRED FROM` rows for holdings,
     but **sum them as cash deposits**.
   - Options = symbol starts with `-` or action contains `CALL`/`PUT`.

## Multiple / partial exports (Fidelity gives inconsistent files)

The user tends to accumulate many overlapping exports in `~/Downloads`. Observed:
- `History_for_Account_*.csv` — full **trade** log, but no dividends and can
  **miss cash transfers** (one was missing a $1,000 deposit).
- `Accounts_History*.csv` — layout has extra `Account`/`Account Number` columns;
  some are trade-only, some are cash/dividend-complete, some are **stale**
  (end weeks before the current portfolio date), some go back to **Sept 2025**.

Rules `generate.py` follows (don't regress these):
- **Header-aware parsing** (`_colmap`): map columns by header name, never fixed
  indices — the two layouts differ by two columns.
- **Anchor only to exports reaching the latest date** (= portfolio snapshot date).
  A trade export ending earlier than current holdings would corrupt the
  reverse-reconstruction, so stale ones are ignored for the timeline.
- Among compatible exports, pick the **trade source** (most stock buys) and the
  **cash source** (most EFT+dividend rows) *independently*. Warn if deposit
  totals disagree.
- Deposits are window-scoped (consistent with the dashboard window). For the
  **lifetime** deposit total, union EFT rows across *all* exports deduped by
  `(date, action, amount)` — this is a slight lower bound (collapses any genuine
  same-day-same-amount duplicates, and months whose exports lack cash rows are
  missing). As of the May-2026 data, lifetime EFT ≈ **$48,884** since 09/2025,
  vs only $5,171 inside the 2-month dashboard window.

Return rate is **unaffected** by any of this: TWR uses holdings×price, never cash
flows. A missing/extra deposit only changes the displayed deposit KPI.

## ⚠️ The single most important gotcha: the history is INCOMPLETE

The history export only covers a ~2-month window and is **not** from account
inception. Many positions (NVDA, GOOGL, VOO, TSLA, MRK, XIACY…) already existed
before the first row. If you naively accumulate buys−sells you get **negative**
share counts for legacy names.

**How we handle it (anchor + reverse-reconstruction):**

- `opening_qty = final_shares (from Portfolio CSV) − net_change_in_window`
- If `opening_qty > 0`, the stock has a **legacy lot** (flag `hasLegacy`).
- We seed that opening lot at an **estimated cost = the real market price on the
  first in-window date** (from Yahoo Finance). Clearly labeled "含旧仓 / 成本按
  当日市价估算" in the UI. This is an approximation and only affects **realized**
  P&L for legacy names.

This is why we anchor to the Portfolio CSV instead of trusting accumulated
history alone.

## Calculation methodology (the two P&L numbers)

| Metric | Method | Accuracy |
|--------|--------|----------|
| **Unrealized P&L** | Straight from Portfolio CSV `Total Gain/Loss Dollar` | Broker-exact |
| **Realized P&L (window)** | Average-cost method walked over the transaction list, seeded with the legacy opening lot | Exact for full in-window round-trips; **estimated** where a legacy lot is involved |

Average-cost engine per stock (chronological):
- BUY: `qty += q; cost += |amount|`
- SELL: `avg = cost/qty; realized += (price − avg)·q; cost −= avg·q; qty −= q`
- Running `pos` and `avg` after each row are stored for the table & the orange
  step-line on the chart.

## Prices (Yahoo Finance via `yfinance`)

- Fetched **once, batched** for all tickers (`yf.download(list, group_by="ticker")`)
  to avoid 429 rate-limiting. Single requests get throttled fast.
- Window = `first_trade_date − 4d` … `last_trade_date + 1d` (yf `end` is exclusive).
- Cached to `output/prices_cache.json`; `--no-fetch` reuses it (offline).
- `auto_adjust=True` (split/div-adjusted closes). Sanity check: NVDA 5/28 close
  matched the Portfolio CSV last price exactly.
- `price_on(sym, date)` returns the close on/before a date (for legacy cost +
  current price of exited names).
- Tickers are **derived from the CSVs at runtime** — never hard-code the list,
  or new stocks silently get no price line.

## The HTML/SVG visualization

- Single template string in `generate.py` (`HTML_TEMPLATE`), data injected by
  replacing the `__DATA__` token with `json.dumps(payload)`. **Not** `.format()`
  / f-strings — the template is full of JS `${...}` and `{}` that would break
  string formatting.
- Charts are **hand-rolled SVG** (no Chart.js) so the file works fully offline.
- Per stock: gray = market price line (Yahoo), orange dashed = running avg cost
  (step line), blue dashed = current price, green/red dots = buy/sell with
  **radius ∝ √(trade amount)**. Hover → fixed-position tooltip.
- Date range is read from `S.dateRange` in JS — no hard-coded dates, so it
  follows whatever window the new CSVs cover.

## How to regenerate when CSVs change

```bash
# drop the new exports in ~/Downloads, then:
python3 generate.py
# or be explicit:
python3 generate.py --portfolio /path/Portfolio_Positions_X.csv --history /path/History_X.csv
# offline (reuse cached prices):
python3 generate.py --no-fetch
```
Auto-detect picks the **newest** file matching `Portfolio_Positions*.csv` and
`History_for_Account*.csv` in `--input-dir` (default `~/Downloads`).
Output → `output/portfolio_dashboard.html`.

## Validation checklist after a rebuild

1. Script prints `got N/N tickers` (no missing). If some miss, that ticker has
   no price line — usually a delisted/renamed symbol; acceptable, note it.
2. `market value` printed ≈ sum of Portfolio CSV current values (held names).
3. `unrealized` printed == sum of Portfolio CSV total gain (held names).
4. Open the HTML; confirm `DATA` parses (no JS console errors), a held stock
   shows price line + markers + avg-cost line.

## Portfolio overview: net-worth curve + return vs benchmarks (IMPLEMENTED)

The "📊 组合总览" view (`payload["series"]`, built in `build_payload`):

- **Daily holdings**: `shares_after(sym, d) = final_shares − Σ(signed qty of
  trades dated after d)`. Works for legacy AND exited names (anchored to the
  Portfolio CSV), so every day's holdings are exact.
- **Net-worth curve**: `value(d) = Σ shares_after(sym,d)·close(sym,d)`. This is
  **equity only** — no cash, no options (those need separate, less reliable
  data). Labeled as such in the UI.
- **Time-weighted return (TWR)** — the right metric to compare against an index
  when the user keeps depositing/buying. Daily return revalues *yesterday's*
  holdings at *today's* prices, so same-day buys/sells and deposits don't
  distort it: `r_t = pval(d, shares_date=prev) / value(prev) − 1`, then chain
  `Π(1+r_t)−1`. Do NOT use (value_today/value_yesterday) directly — that would
  count fresh cash as "return".
- **Benchmarks**: fetch `^GSPC` (S&P 500) and `^IXIC` (NASDAQ Composite) and
  normalize cumulative % from the first axis day. The market-day axis comes from
  `^GSPC` trading days. Benchmarks are added to the fetch list but never become
  "stocks" (they're not in txns/cur).
- Summary exposes `curReturn`, `spReturn`, `nasdaqReturn`, `netWorthNow/Start`;
  the UI also shows **excess return (alpha) = curReturn − spReturn**.
- Generic `svgLines(ser, defs, opts)` JS renders both charts (area for $,
  multi-line + zero baseline for %).

Sanity numbers from the May-2026 dataset: net worth $80.9k→$109.4k, portfolio
TWR +23.4% vs S&P +19.2% (alpha +4.1%) vs NASDAQ +29.4%.

## Extension ideas (not yet built)

- Include cash & option mark-to-market in net worth (needs reliable cash-balance
  history + option price history — both currently messy).
- Money-weighted return (IRR/XIRR) as an alternative to TWR.
- Export per-stock chart to PNG, or full data to Excel.
- Sector grouping / allocation treemap.
- True realized P&L if a full-history export (from inception) becomes available
  — then drop the legacy-estimate seeding entirely.

## Environment notes

- Python 3, `yfinance` (`pip install yfinance`). Only needed at generate time.
- Yahoo endpoint returns 429 on bursty single fetches → keep the batched call.
- macOS: `open output/portfolio_dashboard.html` to view.
