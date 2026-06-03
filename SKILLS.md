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

## Trade Journal + Investor-Maturity engine (IMPLEMENTED)

A behavioral "maturity engine": per-position thesis/plan/emotion tags, plan-
adherence analytics, a process-not-profit **maturity score**, and a weekly
review. Two surfaces — Overview "交易日志" tab (the cockpit) and each stock's
"日志" tab (the 1-minute editor). Designed by an agent-team workflow that read
Thaler; grounded in Edgewonk/TradeZella plan-adherence + emotion tagging.

- **Decision quality ≠ outcome quality** (the core anti-vanity move): the score
  measures *process* (did you write a thesis, set a plan, follow it, stay calm,
  use a checklist) — never P&L. The hero number is `var(--txt)`, bars are amber
  `#E8B339` ONLY. Green/red (`--green`/`--red`) is forbidden anywhere on the
  score — they stay reserved STRICTLY for P&L sign elsewhere. (Enforced by a
  smoke-test assertion that no `#4FB286`/`#E5707A`/`var(--green|red)` appears
  near `hero-fig`.)
- **Five components**, mean of the non-pending ones ×100 → `maturityScore()`:
  `thesisCov` (论点覆盖), `jrnCov` (日志覆盖), `padh` (计划遵守率), `ckl`
  (检查清单), `emo` (情绪克制). **Deliberate asymmetric denominators** (see the
  maintainer comment): *coverage* uses the **held** denominator (you can't get
  credit for un-journaled positions), but *behavior rates* use the **journaled**
  denominator (only judge what you actually logged). The lowest non-pending
  component is surfaced as the "最弱一环" to improve next.
- **No fake 0/100** (no-data honesty): with zero entries `maturityScore()`
  returns `null` and the card renders a "起步" state — never a discouraging fake
  0 before the user has begun. `maturityBand()` labels 主要靠运气 / 成型中 /
  成熟 / 大师 once there's data.
- **Killer stat** (`killerStatCard()`): mean `unrealPct` of **在计划内** vs
  **计划外** decisions, against the held-mean baseline — the single number that
  shows whether following your own plan paid. **n<3 per bucket → suppressed**
  (shows a "样本不足" note, never a noisy 1-sample claim).
- **Emotion → outcome** (`emotionOutcomeCard()`): groups entries by emotion tag
  (destructive FOMO追高/报复/无聊 vs constructive 冷静/坚定/纪律) and shows mean
  outcome per tag — makes "报复性交易亏钱" visible in your *own* data.
- **Converging-evidence nudges** (in `positionJournalEditor()`): a soft chip
  fires only when two signals agree (e.g. disposition pattern + 计划外, or
  conviction≥4 with no stop set) — never a single-signal alarm.
- **Weekly review** (`weeklyReview()`, `ptrak.review.v1` keyed by ISO week): auto
  facts (`.badges`) + 5 free-text prompts (best/worst/lesson/do/avoid). Thaler:
  scheduled reflection counters hindsight bias.
- **Un-journaled worklist** (`unjournaledWorklist()`): held names with no entry,
  each a `.jwork` row that jumps to that stock's editor (sets `ptrak.seg.stk=
  'journal'`, selects the symbol, re-renders) — closing the coverage gap is one
  click.

Storage: **per-position, localStorage, single-device** — `ptrak.journal.v1`
(entries keyed by symbol) + `ptrak.review.v1` (weekly notes). State this to the
user: the journal does **not** sync across devices/browsers and is not in git.
`journalLoad`/`journalSaveEntry`/`journalClearEntry` + `reviewGet`/`reviewSave`
mirror the rebal persistence pattern. Wiring re-renders **only** its own seg
(`wireJournalTab`/`rerenderJournalTab` on Overview, `wireJournalEditor(s)`/
`rerenderJournalEditor(s)` per stock) — never `renderOverview`/`renderDetail`
(which would reset the active tab). `renderDetail()` resets `journalDraft=null`
so a per-symbol draft never leaks across stocks.

**ZERO Python / payload footprint** — pure presentation over the existing
payload, so the equity verification gate stays byte-stable. Validate: `node
--check`; mocked-DOM smoke (`localStorage` stub) that `journalCard`,
`positionJournalEditor(s)`, `maturityScore` (empty→null, post-save→number),
`killerStatCard` (n<3 suppressed) render without throwing and no P&L color leaks
onto the score. Plain-Chinese GLOSS tooltips: `thesisCov, jrnCov, padh, ckl,
emo, killer, emoOut`.

Scoped out (v1.1 follow-ups): conviction→outcome split, journaled-vs-unjournaled
performance split, a past-weeks history browser, surfacing orphan entries (logged
for a name no longer held), cross-device sync.

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

## Decision scorecard (IMPLEMENTED)

The **决策一览** tab — now the DEFAULT overview segment (placed first). One row
per held name that joins the four signal families the dashboard already computes
but had SCATTERED across the 持仓信号 / 风险 / 行为决策 / 再平衡计划 tabs:
technical (fib state/momentum/RSI/last cross), risk (riskPct + the risk−weight
gap bar), behavioral (per-name bias chips), and rebalance drift. Book grounding:
Thaler's narrow-vs-broad framing (Problem 1 vs 2, p.1582) — holdings must be
evaluated JOINTLY in one frame, not piecemeal; and display order/salience is
itself the intervention (p.1595), so it's the default-first frame.

- **关注度 (attention)** = a per-row COUNT of red signals (RSI>70, risk gap>10pp,
  any alert-level bias, |rebalance drift|>band). It is the **default sort key
  only** — explicitly NOT a score and NEVER a buy/sell rating (the load-bearing
  not-advice guardrail). Labeled 关注度/需复盘.
- **Python**: one additive field — `behavior.biasBySym` (sym → [{id,level}]),
  built in `analyze_behavior` from the symbol-prefixed `examples` of GENUINELY
  per-name flags (concentration/disposition/sunkcost/recency/anchoring) at level
  alert/watch only. When the data is clean a name gets no chip (honest — today
  only the 5 concentration names carry one; disposition/recency are "good").
  Don't lower the threshold to manufacture chips, and don't attach account-level
  flags (overtrading) to rows.
- **JS**: `scorecardCard()` (modeled on `positionSignalsCard`) + a shared
  read-only `rebalDriftMap()` (reuses `rebalUniverse/rebalTargets/rebalLoad` so
  the scorecard and the planner agree on drift). Reuses every existing helper
  (FIBCOL/momColor/cls/fmt, .scroll>table, .fbar/.z/.p, .chip, deep-link). Risk
  cells degrade to "—" for names absent from `risk.contrib`; drift cells "—" when
  no target (e.g. inverse-vol disabled). No new CSS, no new payload beyond
  biasBySym, no parse change.
- Insight it surfaces (joint view): on the Jun-01 data VOO/QQQ/MU each hit
  attention 2 (RSI>70 overbought AND concentration-flagged) while the largest
  holding NVDA is only 1 (RSI neutral) — a cross-family pattern invisible in any
  single tab. Each row deep-links to the per-stock detail. 非投资建议.

## Whole-account view (IMPLEMENTED)

Top of the **净值 · 全账户** tab: folds the cash + option mark-to-market the
dashboard used to ignore into a true account picture, and makes the hidden
derivative leverage salient. Book grounding (agent-team design): which slice you
DISPLAY is itself a Supposedly-Irrelevant-Factor (salience, p.1595); money is
fungible so net worth must sum all buckets (law of one price, p.1588); state the
unit of account explicitly (footnote 9, p.1592); a small NET option mark must not
mask large GROSS exposure (dominated-pair framing, p.1582).

- **`parse_account_extras(path)`** — a SEPARATE additive parser (never touch
  `parse_portfolio`, which `sync.py` verifies to the cent). Reads exactly the rows
  the equity parser skips: cash (`**`), option legs (`-`, with col-7 Current Value
  = the MARK), and `Pending activity`. Sign/side from the MARK, NOT the `-` prefix
  (every option symbol starts `-`; a long call is `-...C175` too). `len(r) < 8`
  guard (the pending row is 14 cols). Captures the CSV "Date downloaded" line as
  `asOf` (dynamic — no hardcoded date).
- **Whole-account net worth = equity + cash + Σ(option mark) + pending**, all
  broker-exact. On Jun-01: **$115,444.85** = 113,790.76 + 1,333.09 + 269.00 +
  52.00. New `summary` keys only (`accountNetWorth/cashTotal/optMarkNet/
  optMarkGross/optPctEquity/pendingTotal`) + `payload.account`.
- **CRITICAL naming**: `optMarkNet` (current MTM, +269) is a DIFFERENT number from
  the pre-existing `summary.optNet` (history cash-flow, −912.53) and
  `payload.options` (16 history legs) — never reuse those for MTM (a real
  collision bug). Verified both stay untouched.
- **`wholeAccountCard()`**: hero tiles + a dollars-first composition bar + the
  exact identity line + a per-account cash sub-table.
- **`optionsExposureCard()`** (the salience deliverable): GROSS option market
  value **$53,749 ≈ 47% of equity** shown LARGE; the tiny net +269 demoted; the 4
  legs on a shared zero line (short −$22,950 bar visibly long). The TER Jan-2028
  calendar + QQQ Jun-2026 vertical.
- **Honesty (load-bearing)**: it's MARK, not delta/notional/Greeks; the margin
  DEBIT balance is NOT in the export (only positive money-market cash), so the
  total may overstate free cash; true leverage is larger and not computable here.
  Never print a leverage ratio/delta/notional. Risk lens & rebalancer stay
  EQUITY-ONLY (option Greeks/margin unavailable) — their old "低估你的真实杠杆"
  notes now DEEP-LINK to this view instead of just apologizing.
- Validate: `parse_account_extras` unit asserts (cash 1333.09 / optMarkNet 269 /
  optMarkGross 53749 / pending 52 / 4 legs); the **sync.py equity gate must still
  match** (additive keys are inert); anti-collision assert (`optNet` still
  −912.53); `node --check`; mocked-DOM smoke incl. the `DATA.account==null`
  empty-string path.

## Allocation / Concentration X-ray (IMPLEMENTED)

The **结构** tab (after 风险). Aggregates holdings into asset-class and theme/
sector buckets to surface the hidden CORRELATED bet — for this user, ~42% of
dollars but ~61% of risk is one **半导体** theme across 13 names. Book grounding:
narrow-vs-broad framing (Problem 1 vs 2, p.1582) — do the aggregation the user
won't; correlation neglect (N tickers ≠ N bets); naive 1/N diversification &
the diversification illusion (Benartzi–Thaler); and "structure ≠ correctly
priced" (factor-of-two, p.1588).

**Classification is HYBRID and NEVER fabricated** (CLAUDE.md anti-hallucination +
Thaler's "fudge factor" p.1594), resolved per held name in priority:
1. `CURATED_THEME` — fact-checked vs live yfinance .info on 2026-06-02; its ONLY
   job is the documented GROUPING that collapses chips + "Semiconductor Equipment
   & Materials" + the DRAM memory-ETF into one 半导体 super-theme. Grouping-only;
   never overrides a live fetch except to collapse.
2. `asset_class()` for ETFs/cash (宽基指数ETF / 主题ETF / 杠杆 / 商品 / 现金) —
   derived from sym+name, zero new data, always available (the floor).
3. **Real fetched** sector/industry via `fetch_sectors()` → `sector_to_theme()`
   (reads .info verbatim, translates the sector to Chinese; never guesses).
4. literal **`未分类`** for any miss — rendered as a REAL greyed bucket WITH its
   weight (never dropped → Σ stays 100%, no exposure hidden).

**`fetch_sectors(tickers, no_fetch, ...)`** mirrors `fetch_prices`' --no-fetch/
cache/try-except contract, caches to `output/sectors_cache.json`, and is
**hang-bounded**: get_info() has no timeout arg, so it uses
`ThreadPoolExecutor.result(timeout)` + `shutdown(wait=False)` + a total wall-clock
budget. Only fetches names MISSING from cache and NOT curated (steady-state ≈ 0
calls); merge-on-write so a partial fetch never erases good labels. ETFs return
sector=None (expected) → handled by the asset-class layer. Worst case (Yahoo
down, cold cache) the x-ray degrades to the asset-class floor + 未分类 — truthful,
never fabricated. Provenance (Yahoo行业 / 人工标注 / 资产类别 / 未分类 + asOf) is
shown so the user sees what's live vs cached vs unknown.

**`build_alloc()`** (pure, null-safe) → `payload.alloc`: byAssetClass, byTheme
(weight + aggregated **riskPct from `risk.contrib`** + gap bar), and theme-level
**HHI + effective-N (1/HHI)** in dollar and (if every bucket has risk) risk space
— the honest "名义 28 只 → 主题有效 ≈ 3" statement. Theme weights use the
marketValue basis to reconcile with risk.contrib's 100%. `structureCard()` is a
pure renderer (3 cards), reusing .fbar/.scroll/.badges; risk-space HHI shown only
when complete; excluded-<30d names footnoted; deep-link to 净值·全账户 for option
leverage. **Strictly additive — no summary write — so the sync.py equity gate is
unaffected (verified: still matches the broker CSV to the cent regardless of
fetch outcome).** `output/sectors_cache.json` is committed like prices_cache.

## Friendly UX & interaction layer (IMPLEMENTED)

A presentation-only "choice architecture for the tool itself" pass (designed by
an agent-team workflow reading the book): make-it-easy / reduce friction (p.1595),
simplification & salience, plain framing over jargon (p.1582). **Strictly
additive, inside `HTML_TEMPLATE` only — no Python/payload/summary change, so the
sync.py equity gate is untouched.** Six tiers, each independently revertible:
1. **Scrollable seg-rail** — `.seg-rail` got `overflow-x:auto; scrollbar-width:none`
   + `button{flex:none;white-space:nowrap}` + tighter mobile padding; the 9-tab
   (and 3-tab detail) rails now scroll horizontally instead of overflowing.
2. **`title=` tab hints** — every overview & detail tab button has a one-line
   "what question does this answer" native tooltip (data-seg keys unchanged → all
   deep-links/segWire intact). Plus `title=` on the dense scorecard jargon headers.
3. **Plain-language KPI labels** — badge labels lead with the human phrase, term
   secondary (e.g. "你的钱实际经历的收益 (资金加权 MWR)"). Strings only; the
   `cls(-behaviorGap)` inverted-color logic is NOT touched.
4. **Glossary tooltips** — `GLOSS` map + `gl(k,l)` wraps jargon (TWR/MWR/XIRR/HHI/
   有效持仓数/beta/回撤/动能/RSI/共振…) in a `.gl` dotted-underline span; ONE
   document-delegated controller reuses the existing `#tt` element (adds a `gl-tt`
   class; `pointer-events:auto` only on that class) — hover on desktop, tap-to-pin
   on touch, Esc/click-away to dismiss, Enter/Space on a focused term. `gl()`
   degrades to the bare label for unknown keys (typo-safe). `bindMarkers` clears
   `gl-tt` so the trade-marker tooltip keeps its mono look.
5. **Persistence + onboarding + keyboard** — last-viewed sub-tab persists per
   context (`ptrak.seg.ov` / `ptrak.seg.stk`) via `restoreSeg()` (clicks the saved
   button after render; deep-links still win since they click later); a dismissible
   `#onboard` "从这里开始" strip (a non-`.card` div so the riseIn stagger is
   unaffected; `ptrak.onboard.v1`); rows are `tabindex/role=button` with Enter/Space;
   a document keydown gives Esc→home (defers to an open tooltip), Esc-in-search→clear,
   ←/→ to cycle seg tabs when one is focused; `:focus-visible` amber outlines.
6. **Hygiene** — friendlier empty state (names the query), plainer search
   placeholder, dropped a decorative glyph.

localStorage keys are namespaced `ptrak.*` and mutually independent —
`ptrak.seg.ov`/`ptrak.seg.stk` (active sub-tab), `ptrak.onboard.v1` (onboarding
dismissed), `ptrak.rebal.v1` (rebalance rule), `ptrak.journal.v1` (per-position
trade journal), `ptrak.review.v1` (weekly review); all reads/writes are try/catch
(private-mode degrades to today's behavior).
**Do NOT** soften/remove any 非投资建议 line, "怎么读" note, or Thaler citation —
friendliness = surfacing meaning on demand, never deleting honesty. Validate with
`node --check` on the extracted `<script>` after every change (nested template
literals — a stray backtick breaks silently) plus a mocked-DOM smoke test of
`onboardStrip()/restoreSeg()/ovGo()/renderOverview()` and the keydown/glossary
handlers.

## First-glance insight banner + masthead returns (IMPLEMENTED)

From a 5-persona new-user usability test (scored the design 5/10: great content,
buried — nothing answered the owner's questions at a glance; the default opened on
the densest table; the KPI strip was 8 dollar cells with no %). Fixes (all
presentation-only, computed from existing payload):
- **`insightBanner()`** — a "今日要点" card rendered ABOVE the overview seg-rail
  (after `onboardStrip()`), answering the owner's questions in 4 plain-Chinese
  lines with `ovGo()` links to the detail tab: (1) value + unrealized + 区间收益
  vs 标普 **and the Nasdaq-lag nuance**; (2) largest theme % money / % risk + the
  buried **effective-N** ("≈2 笔独立押注"); (3) **gross option exposure** ≈ % of
  equity (was invisible — only the misleading −$912 net showed); (4) the MWR-vs-TWR
  timing verdict in words. Reads S/alloc/account; null-safe.
- **KPI masthead** gained 区间收益(TWR) / 超额 vs 标普(pp) / 期权毛敞口 tiles, so
  the first row answers "am I up & vs market" (it was all dollars before).
- XIRR tile labeled "数月年化·非预测" inline (not hover-only) so +48% isn't
  misread as a forecast; 指数对比 gave 纳斯达克 its own color (#6E9CA6) — it shared
  amber with 我的组合, an unreadable two-line clash.
Validate: sync gate + node --check + mocked-DOM render.

**Follow-up pass (mobile-first + the deferred items + a second agent's advice).**
A re-test scored it **5 → 7.5 (mobile 4 → 7.5)**; the one high-severity miss it
caught: `#kpis` is a DOM sibling ABOVE `.wrap`, so `.right{order}` couldn't lift
the banner above the KPI ledger on a phone. Fixed by moving the banner to a
**top-level `#insight` slot above `#kpis`** — `renderOverview()` populates it,
`renderDetail()` clears it (`#insight:empty{display:none}`) — so the plain-language
answer is the literal first paint on every viewport. Also: phone trims `#kpis` to
the 5 essentials (`.kpi:nth-child(n+6){display:none}`) and the 12-col 决策一览 to
5 priority columns + sticky ticker col; the 9-tab rail **wraps** on a phone (no
hidden tabs); `#insight`/overview ordered before the holdings rail; a11y
(role=tablist/tab + aria-selected via `segWire`, aria-labels); the two longest
怎么读 walls folded into `<details>`; hero relabeled "股票市值 (不含现金/期权)";
banner gained a "下一步 → 最该看一眼 <top-risk> / 看再平衡动作清单" line, a
"显示新手指南" restore link, a gross-option qualifier (毛额·市价·净≈…), and a
verdict-word timing line (no confusing raw −pp). Softened tab/heading to
"斐波那契·技术".

Still deferred (genuinely new functionality, not polish): true nested 9→5 tabs,
desktop column-priority + show-all toggle, multi-holding compare, sortable headers,
chart zoom/brush, export, URL deep-links, trade-dot touch tooltips.

## Interactive charts & web-design pass (IMPLEMENTED)

The marquee interaction upgrade — every hand-rolled SVG line chart is now
hoverable (the charts were static except trade dots). Architecture (agent-team
design): each builder (`svgLines`/`chart`/`nwChart`/`fibChart`) keeps ALL its
`xs()/yc()` geometry and additionally (1) stamps the root `<svg>` with
`id`+`class="xh"`+`data-x0/x1/ml/pw` (the linear x-map in user units), (2) appends
a hidden `<g class="xg">` crosshair (`.cx` line + `.cxd` dot) and a transparent
`<rect class="xhit">`, and (3) registers `CHARTREG[id]={dates:[epochs],
rows:[prebuilt innerHTML]}`. ONE delegated controller **`bindCharts()`** (registered
ONCE via `window.__xhWired` — both render fns re-run on every nav) does
pointer→nearest-date inversion (`getBoundingClientRect` + `viewBox.baseVal.width`
+ the data-*), moves the crosshair, and writes the readout into the **existing
`#tt`** element. Critical guards: `CHARTREG={}` is reset at the TOP of
`renderOverview`/`renderDetail` (but **CHARTID never resets** → globally-unique ids,
no mid-transition collision); `place()` early-returns on `e.target.closest('.mk')`
so the trade-dot tooltip keeps sole ownership on the price chart; it calls
`tt.classList.remove('gl-tt')` before writing so it never collides with the
glossary controller; the Esc handler already defers to a visible `#tt`.

The readouts **pre-compute the comparison** (Thaler p.1582 — do the arithmetic for
the user): the index chart shows "超额 vs S&P", net-worth shows "较起点 +$X (%)" +
组合动能, fib shows price/EMA5/EMA21/state. `svgLines` defs take `label` and opts
take `delta:{a,b,label}`.

Also in this pass (all reuse the Graphite-Atelier vars, no theme fork): responsive
chart-text bump under 560px (CSS overrides the baked `font-size` attr — no risky
attr→class swap); coarse-pointer trade-dot radius; a sticky **`#ctx`** context pill
(组合总览 · <tab>) + **`#totop`** back-to-top button (shown on scroll via one
passive listener; `updateCtx()` called from both render fns + segWire). **Presentation
-only — no payload/summary change, equity gate stays green.** Validate: `node
--check` + a mocked-DOM smoke test (`bindCharts` once-guard + idempotency,
`CHARTREG` populates dates+rows and resets per render, `updateCtx`).

## Full-dashboard audit + design system (canonical)

A 7-agent workflow walked every section/tab/panel for consistency + correctness
and scored it **7.5/10** ("a high-floor product one cleanup pass from 8.5–9").
**Canonical "Graphite Atelier" system — keep every new panel inside it:**
- Surfaces `--bg/--bg2/--panel/--panel2`; lines `--line/--hair`; text
  `--txt/--mut/--faint`. ONE chromatic accent `--accent` #E8B339 (amber),
  rationed for live/active/you-are-here. `--green/--red` reserved STRICTLY for
  P&L sign / good-bad — **cash & $0 / −0 are neutral (`--mut`), never green**.
- Fonts: `--f-disp` titles/symbols, `--f-ui` labels/body/notes, `--f-mono` every
  numeral. Components: `.card`+`.dh(.t/.nm)`, `.badges/.badge`, `.scroll>table`,
  `.frow`+`.fbar/.z/.p`, `.chip`, `.note`, `.legend`, `.seg-rail`, `.gl`+`#tt`.
- Units: excess-vs-benchmark = **percentage points via `ppf()`** everywhere
  (`+4.54pp`), never `%`; `pct()`/`fmt()` are null-safe (`—`).
- **Foot-gun**: `.card>div[style*="font-weight:650"]` is a substring caption
  normalizer — real hero/severity figures use `.hero-fig`/`.flag-head` (no inline
  `font-weight:650`) to escape it; use `.cap` for new captions.

Bugs fixed from the audit: option expiry/assignment legs (side `?`) now tag
**到期/行权** (were mislabeled 卖出); the `<21`-price-day 斐波那契 tab renders a
**fallback note** (was blank); the normalizer no longer shrinks the 期权毛敞口
hero or strips behavior-flag colors; `$0`/`−0` no longer render green; scorecard
rows are keyboard-operable; disclaimer wording unified to 非投资建议.

**Gap vs the Investor-Maturity-Cockpit vision** (9 exist / 5 partial / 2 missing):
strong on truth/risk-before-return/behavior/plain-language/no-advice/TWR-MWR-index/
concentration/rebalance-rules. Partial: whole-account hero clarity, spread-aware
options, single-system enforcement, a11y parity. **Missing (next worth building):
an entry-time trade journal (thesis + emotion + plan-adherence — the Edgewonk/
TradeZella differentiator) and benchmark breadth (factor/sector/peer).**

**Second audit pass (post-journal) + cleanup — scored 7.5 → ~9.** A larger
review→adversarial-verify→synthesize workflow (85 agents) walked every tab/panel
(incl. the new journal) and confirmed 59 real findings; all 35 prioritized fixes
landed, every one presentation-only (equity gate stayed byte-stable). Highlights:
- **P0** — both rebalance result banners were direct `.card>div[style*="font-weight:650"]`
  children, so the caption-normalizer silently greyed/shrank the sanctioned green
  "无需操作" / red "越界" signal. Fixed by moving them to `.flag-head` (the documented
  opt-out). This is the canonical example of the foot-gun; the nw/cmp captions were
  also migrated to the now-real `.cap` class.
- **P1** — per-stock journal textareas (论点/复盘) only synced to `journalDraft` on
  Save, so tapping any chip (which rebuilds the editor) wiped typed text → added live
  `oninput` sync. The 今日要点 banner showed Nasdaq's absolute return instead of the
  `ppf(pr−nq)` gap. The default 决策一览 tab rendered blank for data-poor portfolios
  → graceful 数据不足 note.
- **Green/red discipline** (the through-line): de-greened cash-flow columns (逐笔/期权
  金额), the dividend KPI, behavior example chips, the emotion-outcome dot, the killer-stat
  numeral, zero-value risk/drift gaps (`g===0`→`--mut`), maxDrawdown (`cls()`), the
  net-worth momentum band (amber-intensity ramp, NOT the technical FIBCOL), and the
  rebalance over/under bars (amber=越界 / grey=区间内). Green/red now means **only**
  P&L sign / good-bad, nowhere else.
- **System hygiene** — killed off-palette ghost hexes (#e6ecf5/#cfd3da/#6E9CA6/#15171B/
  #2A2E36 → tokens), 7px/8px radii → `--r-ctl`/`--r-card`, Nasdaq/RSI teal → in-system
  greys, dead `.kpi .spark` CSS removed, `.cap` actually defined, gap columns get the
  `pp` unit.
- **a11y** — `role=tabpanel`/`aria-controls` linkage in `segWire`, all drill-in rows +
  resonance chips keyboard-operable, holdings-filter `tablist`, journal chips `aria-pressed`,
  form controls `aria-label`, above-the-fold links keyboard + `.ib-lk` keydown, SVG
  `role=img`. **Mobile** — asset-class bars made fluid (`.acrow`, no collapse at 390px),
  trade markers tappable + SELL now a hollow ring (colorblind-safe, shape not hue alone).
- **Vision add (the audit's #1 "build next"):** a **盈亏贡献 (contribution) card** in the
  指数对比 tab — `contributionCard()` ranks held names by |unrealized $| (= value−cost,
  broker-exact), diverging amber/green/red bars by P&L sign, share-% shown only when the
  net dominates the largest single name. Explicitly framed as **dollar-P&L share, NOT a
  TWR attribution** (true per-name TWR needs daily weights the export can't rebuild).
  Answers the brief's #1 question ("which holdings produced my return"). Pure presentation.

Still deferred (v1.1 vision, not yet built): a "本周该练的一件事" focus line in the
maturity card; numeric gap values beside the scorecard's bar-only columns; a
journaled-vs-unjournaled / conviction→outcome performance split; a past-weeks review
browser. Explicitly out of scope (conflict with the static, CSV-fed, equity-gated
architecture): live broker APIs, MCP server, options Greeks/probability-ITM, real-time
quotes, tax-lot/wash-sale engines, factor/style data, cross-device sync.

## Extension ideas (not yet built)

- Export per-stock chart to PNG, or full data to Excel.
- True realized P&L if a full-history export (from inception) becomes available
  — then drop the legacy-estimate seeding entirely.

## Environment notes

- Python 3, `yfinance` (`pip install yfinance`). Only needed at generate time.
- Yahoo endpoint returns 429 on bursty single fetches → keep the batched call.
- macOS: `open output/portfolio_dashboard.html` to view.
