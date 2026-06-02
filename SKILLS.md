# SKILLS.md — Portfolio Timeline Dashboard

> Methodology, conventions and gotchas for regenerating the dashboard when the
> CSVs change. Read this first if you (or a future Claude session) need to
> rebuild, extend, or debug the tool.

## What this project does

Turns two Fidelity CSV exports into one self-contained interactive HTML page
that visualizes **every buy/sell of every stock on a price timeline**, plus
realized & unrealized P&L. Pure HTML + SVG + vanilla JS — no server, no build
step, no external libraries at view time. Just open the `.html` file.

## 🔄 Syncing new exports (the recurring update workflow)

The user periodically drops fresh Fidelity exports in `~/Downloads`
(`Portfolio_Positions_*.csv` + `Accounts_History*.csv`) and asks Claude to read
them and update the tracker. **That whole job is one command:**

```bash
python3 sync.py            # regenerate + verify + log (fetches prices)
python3 sync.py --no-fetch # offline: reuse cached prices
python3 sync.py --open     # also open the dashboard when done
```

`sync.py` wraps `generate.py` (still the single source of truth) and adds two
things `generate.py` alone can't:

1. **A verification gate.** After regenerating, it re-derives held-equity market
   value / unrealized P&L / held count *independently* from the newest Portfolio
   CSV and asserts the written dashboard agrees to the cent. If a holding — or a
   whole account — is silently dropped, the sync **fails loudly** instead of
   shipping a wrong dashboard. (This is exactly the class of bug that hid the
   Rollover IRA; see the multi-account note below.)
2. **A sync log.** Appends a snapshot (value, P&L, TWR, deposits) to
   `output/sync_log.json` and prints the delta vs the previous sync.

There is also a Claude Code skill at `.claude/skills/portfolio-sync/SKILL.md` so
a future Claude session auto-recognizes "sync / update my portfolio" requests.
Everything below is the methodology `generate.py` implements.

## Inputs (the two CSVs)

1. **Portfolio Positions CSV** — `Portfolio_Positions_*.csv`
   - Current holdings snapshot. Columns used: `Symbol`(2), `Quantity`(4),
     `Last Price`(5), `Current Value`(7), `Total Gain/Loss Dollar`(10),
     `Cost Basis Total`(13).
   - Rows to skip: **any money-market core position** (symbol ends in `**`, e.g.
     `SPAXX**`, `FDRXX**` — these are cash, counted separately), `Pending
     activity`, blank symbols, and option rows (symbol starts with `-`).
   - A symbol can appear in **multiple lots** (Cash + Margin) **and across
     multiple accounts** — sum them all. The export spans several accounts:
     brokerage ids start with `Z` (Z20695967, Z33862818, …) **and** a Rollover
     IRA has a *numeric* id (`257937289`). `parse_portfolio` matches account rows
     by an alphanumeric id in col 0 (`^[A-Z0-9]{5,}$`) — **never** a `Z`-only
     prefix, which silently dropped the entire IRA (and the `**` skip must cover
     `FDRXX**`, not just `SPAXX**`). The `sync.py` gate guards against regressing
     either of these.
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
- **Merge ALL exports** (`merge_histories`): each export contributes the dates
  it covers; the same real trade repeats across overlapping exports. Dedup by
  taking the **MAX count per identical-transaction key** across files (never the
  sum) — this preserves genuine same-day duplicate trades while collapsing
  cross-file repeats. Key = `(date, side, symbol, qty, price, amount)`.
- **Continuous-span anchoring** (`continuous_start`): after merging, find the
  largest gap-free span ending at the latest trade. A `>=20`-day hole = an
  export is missing those weeks; reconstructing across it would be wrong, so the
  timeline starts after the last such gap. For the May-2026 data this is
  2026-01-06 (a 64-day Nov2025–Jan2026 hole is excluded). Trades before the start
  are dropped from the timeline (opening lot at the start date absorbs them).
- **Validate**: reconstructed holdings must never go meaningfully negative
  (`pos >= -0.01`); a negative means a buy is missing. The merged Jan–May set
  passes with 0 negatives, confirming completeness.
- **Cash** (`union_cash`): deduped union of EFT/dividend rows by
  `(date, kind, amount)`. Show **window** deposits (KPI, consistent with the
  timeline) AND **lifetime** deposits (since account open). Lifetime is a slight
  lower bound (collapses genuine same-day-same-amount dupes; months whose
  exports lack cash rows are missing). As of May-2026: lifetime EFT ≈ **$48,884**
  since 09/2025; window (Jan–May) ≈ $35,742.

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
# drop the new exports in ~/Downloads, then (preferred — regenerate+verify+log):
python3 sync.py
# raw generator (no verification gate / no log):
python3 generate.py
# or be explicit:
python3 generate.py --portfolio /path/Portfolio_Positions_X.csv --history /path/History_X.csv
# offline (reuse cached prices):
python3 sync.py --no-fetch          # or: python3 generate.py --no-fetch
```
Auto-detect picks the **newest** `Portfolio_Positions*.csv` and **merges every**
`Accounts_History*.csv` + `History_for_Account*.csv` in `--input-dir` (default
`~/Downloads`). Output → `output/portfolio_dashboard.html`.

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

Sanity numbers (latest sync, **Jun-01-2026** snapshot, window 2026-01-06→05-29,
all 4 accounts): market value **$113.8k**, unrealized **+$19.2k**, net-worth
curve $62.3k→$111.2k, portfolio TWR **+10.9%** vs S&P +9.1% (alpha +1.7%) vs
NASDAQ +14.5%. (The net-worth curve is equity-only and ends at the last *trade*
date 05-29, so it sits ~$2.6k below the 06-01 broker market value — the one
trading-day price move, not a bug.) The earlier single-account May snapshot read
differently because it anchored holdings to just `Z20695967`; syncing all
accounts is the correct, consistent picture.

## Fibonacci momentum analytics (IMPLEMENTED)

`compute_fib(items)` runs on **any** `(date, value)` series — each stock's daily
closes AND the whole-portfolio net-worth curve — and returns the same dict:
- **EMA ribbon** 5/8/13/21 (`e5..e21`) — the geometric (~1.6×) spacing gives a
  natural fast/mid/slow layering. Drawn as a state-colored band + 4 lines.
  Verified bit-identical to `pandas ewm(span=n, adjust=False)` (seeded with the
  first value; the first ~n bars are warm-up — expected for that convention).
- **State** per day (Alligator-style): checks **stack order first** so a genuine
  low-volatility trend isn't hidden — `up`/`down` (cleanly stacked EMAs), else
  `range` (ribbon width <0.8% of price → chop), else `mixed` (transition). Shown
  as the bottom color strip and the list-row dot. *(Was: range-first, which
  mislabeled slow steady trends as chop — fixed.)*
- **Momentum** `= 100*tanh((EMA5-EMA21)/EMA21 / MOM_SCALE)` → signed, ±100
  bounded. `MOM_SCALE = 0.06` is a **named constant** referenced by both the code
  and the docstring (they had silently drifted: doc said 0.04, code 0.06).
- **RSI(14)** Wilder's (verified max-diff 0.0 vs an independent reference; zero-
  loss windows read 99.9 not 100.0 — a harmless 0.1 ceiling, ~never hit).
- **golden/death cross** = EMA5 crossing EMA13 — this is the **fast ribbon
  cross**, NOT the classic 50/200-day cross; it's labeled as such in the UI and
  the docstring so users don't expect a long-term signal. `resonance` gates it
  (trend + cross within 3 days + RSI not extreme) to filter false crosses.
- `now` = latest snapshot {state,label,mom,rsi,res} used by list/overview/badges.

UI per stock: "斐波那契动能分析" card (ribbon chart with cross triangles +
resonance rings, momentum oscillator, RSI with 30/70 guides; oscillator/RSI now
draw dashed vertical lines at cross dates via `svgLines` `opts.marks`).

**Portfolio-level Fibonacci (IMPLEMENTED).** `payload["portfolioFib"] =
compute_fib([(p["date"], p["value"]) for p in series])` runs the same engine on
the net-worth curve. Drawn in the overview "组合斐波那契" tab by reusing
`fibChart`/`svgLines` with a pseudo-stock `{prices: ser.map(p=>[date,value]),
fib: portfolioFib}` and a `$k` y-formatter (`fibChart(s, fmtY)`). Plus a "持仓
信号" tab: per-holding table of weight/state/momentum/RSI/last-cross/resonance +
a neutral "技术姿态" string. Guard: `portfolioFib` is `None` if the curve has
<21 trading days.

**Honest framing (keep this in the UI).** There's no strong evidence Fibonacci
periods beat other periods; the real value is the geometric spacing's layering,
not numerology. The panel is labeled a technical-analysis reference, **not
investment advice** — don't let it drift into signal-chasing claims.

## Behavioral decision support (IMPLEMENTED)

`analyze_behavior(stocks, summary, prices, dmin, dmax)` → `payload["behavior"]`,
rendered in the overview "行为决策" tab. Grounded in **Thaler, "Behavioral
Economics: Past, Present, and Future" (AER 2016)** — each flag computes a
concrete signal from the user's OWN trades & positions, with a page-cited
reference and a constructive nudge. **Framed as reflective prompts, NOT trade
advice** (this is the load-bearing constraint — never emit "buy/sell X"):
- **disposition** — Odean PGR/PLR from realized-gain vs realized-loss sells and
  held winners/losers (ratio >1.5 ⇒ sell-winners/ride-losers). *(In the current
  data this is GOOD: PGR/PLR≈0.73 — the user actually cuts losers faster.)*
- **overtrading** — trade count + annualized turnover, cross-checked vs the S&P
  TWR already computed (Thaler: active trading on avg trails the index).
- **concentration** — top weight / top-5 / HHI (self-control & defaults nudge).
- **sunkcost** — BUYs priced below running avg into currently-losing names.
- **anchoring** — SELLs clustered at break-even (|realized|/proceeds <2%).
- **recency** — BUYs made after a >12% 20-day run-up (extrapolation).

Levels `alert|watch|good` drive the card color; `flags` are sorted worst-first.
Detection thresholds are heuristics — keep them transparent (show the numbers),
and keep the disclaimer prominent.

Validate after changes: `node --check` on the extracted `<script>` (nested
template-literals — a stray backtick breaks silently), AND a mocked-DOM runtime
smoke test (stub `document`/`MutationObserver`, then call `portfolioFibCard()`,
`positionSignalsCard()`, `behaviorCard()`, `renderOverview()`, `renderFib()`) to
catch ReferenceErrors that `node --check` can't.

## Risk lens (IMPLEMENTED)

`compute_risk(series, stocks)` → `payload["risk"]`, drawn in the overview "风险"
tab. Adds the gauge the dashboard structurally lacked: **how much risk** earned
the return. Grounded in Thaler 2016 — myopic loss aversion / equity-premium
puzzle (p.1594-95) and the loss-averse value function (p.1592): drawdown frames
performance against the high-water reference the user actually feels, and giving
risk equal visual billing fixes the salience asymmetry (return was loud, risk
silent → overconfidence).

- **Basis = the TWR `ret` series, NOT `series[].value`.** value mixes in
  deposits/trades; using it would report the wrong drawdown (−8.2% vs the correct
  TWR −11.1%). Daily returns are reconstructed from cumulative `ret`:
  `cum_i = 1+ret_i/100`, `r_i = cum_i/cum_{i-1}−1`. (Mirrors the TWR rationale in
  CLAUDE.md — never use value_today/value_yesterday.)
- **Metrics**: annualized vol (`pstdev(r)·√252`) vs S&P, beta (`cov/var` vs S&P),
  max drawdown + peak/trough dates, current underwater %, naive return/vol (rf=0).
- **Underwater curve** (portfolio + S&P) via `svgLines(..., {zero:true,area:true})`
  — the area+zero support already in svgLines gives the red underwater shape free.
- **21-day rolling annualized vol** (portfolio + S&P).
- **Risk-contribution table** (the centerpiece): each holding's *dollar weight*
  vs its *share of portfolio volatility* (marginal contribution `w·(Σw)/√(wΣw)`,
  normalized to sum 100%). Surfaces that **weight ≠ risk** — e.g. on the Jun-01
  data NVDA 24%→30% risk (+5.4), **MU 6%→14% risk (+8.2, a hidden driver)**, VOO
  18%→9% risk (−9.0, the diversifier). Covariance is pure-stdlib (no numpy);
  names with <30 days of price overlap are excluded + footnoted.
- **Scope (state it in the card)**: equity-only & DESCRIPTIVE of realized
  in-window risk — not a forecast, and it excludes cash/margin/options so it
  **understates** true leveraged risk. `None` if <25 days or no holdings.

Validate: `compute_risk` sanity gates — `Σ riskPct ≈ 100`, `maxDrawdown ≤
currentUnderwater ≤ 0`; then the standard `node --check` + mocked-DOM smoke test
(`riskCard()`, `renderOverview()`).

## Rebalancing planner (IMPLEMENTED)

Overview "再平衡计划" tab — a Thaler **commitment device** that turns the
concentration finding into a pre-committed, defaulted plan with a drift-band
nudge. Grounded in the book (designed by an agent-team workflow that read it):
- **Default does the work** (auto-enrollment, p.1595): opens with a fully-applied
  default policy — single-name **CAP 20%** with pro-rata redistribution — so the
  user *accepts/tweaks* a plan, never builds one from a blank form.
- **Planner vs doer + commitment** (p.1585-86): the rule (policy, cap, band,
  glide) is persisted to `localStorage` (`ptrak.rebal.v1`) with a "规则设定于
  <date>" stamp — set when calm, the page holds you to it across reloads.
- **Band = no-action zone** (status-quo bias as ally, p.1595-96): default ±5pp;
  in-band names show a loud green "无需操作" — rewarding the high-turnover user
  for *inaction*. Only out-of-band names get an action row (the SMarT-style
  future trigger).
- **Policies** (computed CLIENT-SIDE in JS — single source of truth, recompute on
  every control change): `cap` (iterative cap+redistribute), `equal` (1/N),
  `invvol` (∝1/annVol risk-parity — **disabled, not silently equal-weighted, when
  any held name lacks vol**; needs the one Python addition `contrib[].annVol`).
- **Action math**: glide to band edge (default, gentler) or to target center;
  Δ$=(target−cur)/100·MV, shares=round(Δ$/price). NOT generally cash-neutral with
  one-sided glides → shows 卖出/买入/净 honestly, not a fake "/2".
- **Reference-point discipline** (p.1582,1592): the view suppresses P&L / cost /
  avg entirely; bars colored by drift *direction*, never by profit — so the only
  on-screen anchor is the target band (defeats the disposition reflex).
- **Honesty**: equity-only (understates leverage), ignores tax/wash-sale/lot/
  commissions ("transaction costs = the all-purpose fudge factor", p.1593),
  whole-share rounding, single-browser. Prominent 非投资建议.

Python footprint is ONE field (`annVol` on each `risk.contrib` row, from the
covariance diagonal). All target/band/action math is JS. `wireRebal()` binds the
controls and re-renders only the `rebal` seg (never `renderOverview`, which would
reset the active tab). Validate: `capTargets` sums to 100 with no name > cap;
`node --check`; mocked-DOM smoke test (`localStorage` stub) of `rebalancePlanner`,
`capTargets`, persistence, and reference-point discipline (no P/L strings leak).

Scoped out for now (follow-ups): editable per-name custom targets, relative-band
mode, glide scheduler/calendar, cross-device sync, print/export.

## Money-weighted return + dollar bridge (IMPLEMENTED)

Added to the default **净值** overview tab (designed by an agent-team workflow
that read the book). The user deposits heavily/irregularly (~58% of base
mid-window), so TWR (the deposit-neutral "strategy" metric) and what their own
dollars earned diverge — the time-weighted vs dollar-weighted **behavior gap**.
Book grounding: putting MWR *beside* TWR is the de-bracketing move (narrow
framing, p.1582); the dollar bridge defeats %-money-illusion on a moving base
(p.1595) and honors mental-accounting buckets (p.1592).

- **`xirr(flows)`** (pure stdlib, before `compute_risk`): Newton + bisection
  fallback; returns `None` (caller shows "—") when undefined — <2 flows, span
  <14d, no sign change, or non-convergence. Never fabricates.
- **Equity-book flows** (NOT account-level — cash/margin over time untracked,
  which would be Thaler's "fudge factor" p.1593): `−V0` at dmin (V0 =
  `series[0].value`, equity market value at start) + each in-window trade's
  **already-signed** amount (BUY −, SELL +; **only trades dated > dmin**, else
  they double-count V0) + dividends + terminal (`marketValue`) at dmax+2d.
- `summary.mwrPeriod` (period MWR), `mwrAnnual` (annualized XIRR, **suppressed if
  span <60d**), `behaviorGap = curReturn − mwrPeriod`. Current data: TWR +10.9%,
  MWR +16.7%, XIRR +47.5%, gap **−5.8pp** (dollars *beat* the strategy → deposits
  were timed before up-moves; flagged "good", with a single-window-luck caveat).
- **Tiles**: TWR / MWR / XIRR / gap, each %-with-a-note. The gap tile is colored
  by `cls(-behaviorGap)` ON PURPOSE (+gap = dollars lagged → red); a code comment
  says so — don't "fix" it to `cls(behaviorGap)`.
- **`bridgeCard()` (真金白银桥)** — plain-DOLLAR, EXACT (no fudge): the identity
  `持仓成本 + 未实现 = 当前市值` (broker-exact) plus the four P&L buckets
  (unrealized券商精确 / realized含估算 / dividends / options) as shared-zero-scale
  diverging bars summing to a 合计盈亏 line. Anchored on COST, never the
  market-value V0 — anchoring on V0 double-counts pre-window embedded gains and
  produced a misleading ~$4.9k residual; the cost anchor reconciles to the cent.
- TWR chain is untouched (additive only). Validate: `xirr` unit cases
  (−100→+110@1y≈0.10; no-sign-change/single/short → None); `capitalIn`-style
  identity gates; `node --check`; mocked-DOM smoke test incl. the `behaviorGap=null`
  em-dash path.

TWR judges the *strategy*; MWR + the bridge judge *your money*; 指数对比 judges
*vs the market* — three different questions, co-located so they can't be framed
in isolation. Equity-only; 非投资建议.

## Extension ideas (not yet built)

- Include cash & option mark-to-market in net worth (needs reliable cash-balance
  history + option price history — both currently messy); would also upgrade the
  risk lens from equity-only to whole-account/leverage risk.
- Export per-stock chart to PNG, or full data to Excel.
- Sector grouping / allocation treemap (needs sector metadata — not fetched yet).
- True realized P&L if a full-history export (from inception) becomes available
  — then drop the legacy-estimate seeding entirely.

## Environment notes

- Python 3, `yfinance` (`pip install yfinance`). Only needed at generate time.
- Yahoo endpoint returns 429 on bursty single fetches → keep the batched call.
- macOS: `open output/portfolio_dashboard.html` to view.
