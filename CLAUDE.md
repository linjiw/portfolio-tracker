# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A zero-dependency-at-view-time portfolio dashboard generator. It turns two
Fidelity CSV exports (a current-holdings snapshot + a transaction history) into
one self-contained interactive HTML file (`output/portfolio_dashboard.html`) —
hand-rolled SVG + vanilla JS, no server, no build step, no view-time libraries.

There is **no test suite, no linter, no build**. "Correctness" is enforced at
generate time by the `sync.py` verification gate and by `node --check` on the
emitted JS (see Validation).

## Commands

```bash
python3 sync.py            # PREFERRED: regenerate + verify vs broker CSV + log a snapshot
python3 sync.py --no-fetch # offline — reuse output/prices_cache.json (skips Yahoo)
python3 sync.py --open      # also open the dashboard when done
python3 generate.py         # underlying generator alone (no verify gate, no log)
python3 generate.py --portfolio P.csv --history H.csv   # explicit input files
```

Default behavior auto-detects the **newest** `Portfolio_Positions*.csv` and
**merges every** `Accounts_History*.csv` + `History_for_Account*.csv` in
`~/Downloads` (override with `--input-dir`). The only dependency is `yfinance`
(`pip install yfinance`), needed only when fetching prices.

When the user drops fresh exports and asks to "sync/update my portfolio," the
`.claude/skills/portfolio-sync/` skill should auto-trigger; the job is just
`python3 sync.py`.

## Architecture (the big picture)

The whole pipeline lives in **`generate.py`** (monolithic by design) and runs in
one pass inside `main()`:

1. **Parse** — `parse_history()` (header-aware via `_colmap`; handles two
   different Fidelity layouts) and `parse_portfolio()`.
2. **Merge** — `merge_histories()` unions all history exports, deduping by
   **MAX count per identical-trade key** across files (never sum) so genuine
   same-day duplicate trades survive but cross-file repeats collapse.
3. **Anchor the timeline** — `continuous_start()` finds the largest gap-free span
   ending at the last trade; a ≥20-day hole means an export is missing weeks, so
   the timeline starts after it (trades before are dropped).
4. **Fetch prices** — `fetch_prices()` batches one `yf.download(...)` for all
   tickers + benchmarks `^GSPC`/`^IXIC`; caches to `output/prices_cache.json`.
5. **Build payload** — `build_payload()` reconstructs daily holdings, runs the
   average-cost P&L engine, computes TWR vs benchmarks and the Fibonacci/EMA
   analytics (`compute_fib`).
6. **Render** — `render_html()` injects the payload JSON into `HTML_TEMPLATE`.

**`sync.py`** wraps `generate.py` and adds the two things it can't do alone: an
independent verification gate (re-sums the Portfolio CSV and asserts the written
dashboard agrees to the cent — fails loudly if a holding/account is dropped) and
a `output/sync_log.json` append with a delta-vs-last-sync printout.

`scripts/_scratch/` is superseded exploratory code — **do not use it** (hard-coded
paths/tickers). `generate.py` is the single entry point.

### The core conceptual model: incomplete history + reverse reconstruction

The history export covers only a recent window, **not** account inception. Naively
accumulating buys−sells gives negative share counts for legacy names. So the
**Portfolio CSV is the anchor / source of truth for current holdings**, and
holdings are computed *backward* from it:
`shares_after(sym, d) = final_shares − Σ(signed qty of trades after d)`. A
positive `opening_qty = final − net_in_window` means a legacy lot, seeded at the
market price on the window's first day (an estimate — only affects *realized* P&L
for legacy names). This is why both P&L numbers differ in accuracy:

- **Unrealized P&L** = straight from Portfolio CSV → broker-exact.
- **Realized P&L (window)** = average-cost walk → exact for in-window round-trips,
  *estimated* where a legacy lot is involved.

Return rate (TWR) revalues *yesterday's* holdings at *today's* prices and chains
daily returns, so deposits/same-day trades don't distort it — never use
value_today/value_yesterday directly.

## Non-obvious gotchas (will bite if you don't know them)

- **`render_html` uses `.replace("__DATA__", json)`, NOT `.format()`/f-strings.**
  The template is full of JS `${...}` and `{}` that would break string formatting.
- **Tickers are derived from the CSVs at runtime** — never hard-code a ticker
  list, or new stocks silently get no price line.
- **`fetch_prices` OVERWRITES the cache** with the freshly fetched window. The
  fetch window is `firstTrade−4d … lastTrade+1d`, so the timeline ends at the last
  *trade* date — which can sit ~1 trading day behind the Portfolio snapshot date
  (a small expected gap between the net-worth curve and broker market value).
- **`parse_portfolio` must include ALL accounts.** The export spans brokerage
  (`Z...`) **and** numeric retirement accounts (e.g. a Rollover IRA `257937289`);
  match account rows by `^[A-Z0-9]{5,}$`, never a `Z`-only prefix (that silently
  dropped an entire IRA once). Skip money-market core positions by the `**`
  suffix (`SPAXX**`, `FDRXX**` are cash, counted separately) — both, not just SPAXX.
- **The `sync.py` gate is the safety net for the above.** If it reports MISMATCH,
  the dashboard is wrong — fix `parse_portfolio` before trusting the output.
- **UI labels are in Chinese** (e.g. 组合总览, 斐波那契动能分析, 含旧仓). The
  Fibonacci panel is framed as a technical-analysis reference, **not** advice —
  keep that framing.
- **Personal data:** `*.csv` is gitignored on purpose; the generated
  `output/*.html`, `prices_cache.json`, and `sync_log.json` ARE tracked.

## Validation after any change

1. `python3 sync.py` (or `--no-fetch`) prints `got N/N tickers` and the
   verification block shows `OK` for marketValue / unrealized / numHeld.
2. `node --check` on the `<script>` extracted from the HTML — the template nests
   many template-literals and a stray backtick breaks JS silently.
3. Open the HTML; confirm the payload parses (no console errors) and a held stock
   shows its price line + buy/sell markers + avg-cost step line.

## Where to read more

- **SKILLS.md** — full methodology, merge/anchor rules, P&L math, the Fibonacci
  analytics, and the sync workflow. Start here when extending or debugging.
- **README.md** — user-facing quick start, what the dashboard shows.
