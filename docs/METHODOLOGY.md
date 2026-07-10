# Methodology

This project is built around a strict separation between public framework code
and private account data.

## Data Contract

The generator expects two local broker-export concepts:

- position snapshot: current holdings, quantities, latest broker mark, current
  value, unrealized P&L, and cost basis
- account history: dated buys, sells, deposits, dividends, interest, and option
  transactions

Both parsers are strict and header-driven. They accept arbitrary non-empty
account identifiers, tolerate broker column reordering, sum the same ticker
across accounts, skip known money-market core rows from equity totals, and
treat OCC-format option rows as separate account exposure. Missing required
columns, malformed required numerics, duplicate headers, or an empty recognized
data section stop generation instead of producing a partial dashboard.

## P&L Rules

- Unrealized P&L comes from the latest position snapshot for broker-exact syncs.
- Realized P&L is computed inside the activity window using average cost.
- Positions that existed before the visible activity window are marked as legacy
  lots; their starting cost is estimated from the first available market price.
- Realized-P&L rows carry a confidence and basis scope. Legacy opening basis,
  missing account identifiers, reconstructed inventory shortfalls, and
  cross-account average-cost pooling are disclosed; this is not a broker
  tax-lot statement.
- `--mark-to-market` keeps shares and cost basis from the broker snapshot while
  refreshing held-equity marks from Yahoo prices.

## History and Return Integrity

- The default activity window starts at the earliest known trade. A long period
  without trades is normal inactivity and never truncates history. Use
  `--history-start YYYY-MM-DD` for an intentional later (or earlier inferred)
  analysis start.
- Recognized trade rows require valid quantity, price, and amount fields;
  recognized cash rows require a valid amount. Malformed required numerics stop
  generation instead of silently becoming zero. Blank non-applicable option
  lifecycle fields remain allowed.
- Historical valuation uses only a close observed on or before the valuation
  date. It never fills an earlier date with a future price. General as-of
  lookups allow at most seven calendar days for weekends/holidays; an older
  observation is missing rather than carried indefinitely. TWR,
  counterfactual, benchmark, and MWR-opening valuation paths require an exact
  market-date close and break on a data hole.
- TWR is published from the latest contiguous segment where every non-zero
  holding can be valued both at each close and across each holdings transition.
  Missing-price sessions break the chain and are recorded in `twrQuality`.
- The TWR value path uses split/distribution-adjusted closes. Its return ratios
  are suitable for total-return comparison, but its historical dollar points
  are a normalized analytical scale, not broker account NAV. The current
  holding market value shown in the dashboard comes from the broker snapshot.
- Counterfactual paths stop at the first incomplete branch valuation. A replay
  with failed price coverage is not scored.
- Holding risk contributions use exact common consecutive-session close pairs
  and never forward-fill missing returns. To keep a few short-history names
  from suppressing the entire ledger, the engine may publish a
  value-prioritized covered slice only when it represents at least 80% of held
  equity and has at least 29 common return pairs. Risk and capital shares then
  sum to 100% *within that covered slice*; full-portfolio capital weights,
  coverage percentage, and excluded symbols remain separate and visible. Below
  the 80% gate, the contribution ledger is withheld.
- MWR is an equity-book XIRR, not whole-account IRR. Exact broker buy/sell cash
  amounts are included from the first usable date; known dividends and
  corporate-action cash use their actual dates, and the terminal value uses its
  real snapshot/price date. Only inventory already held before the usable
  window is seeded from an adjusted-close market-value estimate, which
  downgrades `mwrQuality`. Aggregate-only cash timing is also labeled estimated;
  non-conventional cash flows are flagged for possible multiple IRRs. In that
  case the numerical candidate is retained only in `mwrQuality` for audit while
  the headline MWR and behavior gap are withheld. The behavior gap is also
  withheld when TWR and MWR terminal dates differ.

## Verification Gate

`sync.py` generates into a private staging file, then independently re-parses
the position snapshot. It verifies held-equity market value, unrealized P&L,
cost basis, per-symbol shares, cash, pending activity, option net/gross marks,
option P&L and entry cash, option-leg count, account coverage, and whole-account
arithmetic. The independent parser does not share the generator's row parser.
Any mismatch blocks publication and preserves the prior last-known-good
dashboard.

This gate protects against dropped accounts, skipped holdings, and accidental
parser regressions.

## Financial-Status Lens

`scripts/financial_status_score.py` builds a company-level score using a source
cascade:

- FMP when an API key is configured
- yfinance for public Yahoo-style financial summaries
- SEC company facts when `SEC_USER_AGENT` is configured
- local cache or partial data when live sources are unavailable

Scores separate financial quality, earnings-report behavior, next-earnings risk,
market behavior, and data-confidence penalties. API keys stay outside the repo.

All margin, return, yield, and growth inputs have an explicit unit contract.
Source adapters emit decimal ratios (for example, `0.25` means 25%); the scorer
never guesses a unit from magnitude. Numeric zero is a valid observation and
does not fall through to another source or a neutral default.

Debt prefers a reported comprehensive total. If total debt is unavailable,
current and noncurrent components are added only when they share the same
fiscal period; a lone component is not mislabeled total debt. Standard ROIC is
NOPAT TTM divided by average invested capital using a defensible effective tax
rate and approximately year-apart capital observations. If those inputs are not
available, ROIC is withheld rather than substituting operating income over
current capital.

Portfolio weights always use the complete held-equity market-value denominator.
`--symbols` and `--max-symbols` select report rows only; they do not rebase the
selected names to 100%. Selection coverage is recorded in the artifact.

The live FMP TTM, Yahoo summary, and current SEC adapters are current/as-known
sources, not filing-vintage historical snapshots. A historical `--as-of` fails
closed by default. `--allow-non-point-in-time` is an explicit research-only
override; it labels the artifact as using current fundamentals and as ineligible
for backtesting.

## QQQ/TQQQ Decision Map

QQQ is the regime instrument; TQQQ is a tactical execution instrument. The
dashboard calculates QQQ EMA8/13/21/34/55/89, Wilder ATR14, RSI14, slopes, and
distance bands from completed daily observations. A price reclaim with EMA8
still below EMA21 is a separate `repair` state, not a confirmed trend or a
fresh break.

Every TQQQ-dependent value requires the latest TQQQ observation to share the
QQQ as-of date. If dates differ, TQQQ price, five-session return, ATR/trailing
levels, cash-secured-put capacity, CCS ranges, and every new TQQQ/option action
are withheld under `BLOCK_DATA`; QQQ may still describe the market regime. Both
source dates and the execution gate are published.

The QQQ/TQQQ backtest observes signals on a completed bar and fills on the next
close with costs. ATR/EMA warmups must be complete before either tactical or
CCS logic becomes eligible. Synthetic CCS valuations are research scenarios,
not historical fills, and remain excluded from decision-grade evidence.

## SPMO Momentum Sleeve

`scripts/spmo_momentum_sleeve.py` computes trend, ATR, momentum, and relative
strength from split- and distribution-adjusted OHLC history. A new-add `ALLOW`
requires aligned SPMO/benchmark dates plus explicit `ALLOW` labels from both the
technical and decision market-sentinel agents. `WATCH` allows position
management but no new add. Missing, stale, locked, incomplete, or dashboard-
misaligned sentinel context fails closed.

The saved 3xATR stop is scoped to an observed holding lifecycle. It ratchets
only across consecutive active snapshots, clears while flat, and restarts on a
later active snapshot. If a close and re-entry occurred between snapshots, use
`--reset-stop --reset-reason "..."`; each artifact records the lifecycle ID,
reset provenance, observed shares/cost, and the limitation of snapshot-only
position inference.

## Intraday Tape and Market Sentinel

The intraday tape uses only completed 15-minute bars for signals. Daily bars
that are still forming are excluded, Wilder ATR is seeded from true ranges,
flat RSI is 50, and relative volume compares the same elapsed portion of prior
sessions with a minimum sample gate. Session logic is timezone-aware and
includes standard U.S. holidays and recurring early closes.

An event calendar is a required risk input, not optional decoration. Missing,
expired, malformed, or timezone-ambiguous event coverage produces
`BLOCK_DATA`. Off-cycle events stay locked until a complete post-event bar has
closed. The market-sentinel layer may make a tape verdict more conservative,
but it cannot promote `WATCH` to `ALLOW`, lower an already saved stop, invent
option strikes, or describe a call credit spread as a hedge without verified
long exposure.

Sensor and judge artifacts share a run identifier and deterministic hard
gates. A judge response with a mismatched run, contradictory verdict/message,
missing disclaimer, or invalid schema is rejected. State and dispatch files
use private atomic writes and locks. Yahoo bars remain research-grade rather
than an executable exchange feed, and option structures remain `WATCH` until a
current chain supplies exact expiry, liquidity, economics, and max loss.

## Momentum Top-3 Research Backtest

`scripts/momentum_top3.py` treats a month-end close as a signal observation,
not an executable fill. The target basket fills at the next trading session's
close. Existing weights earn the signal-close to fill-close return, drift with
that move, and only then are compared with target weights. Per-side costs are
charged on gross traded notional at the fill close. A missing close for any
held or target symbol skips the entire rebalance instead of inventing a fill.
Within an evaluation segment, a temporary missing held mark does not erase the
intervening return: the next observed close books the cumulative move from the
last valid mark. An unresolved held-price gap at the end truncates that segment
and is disclosed.

Strategy equity begins with an explicit `1.0` initial-capital anchor, so the
first entry cost or first loss is included in total return, CAGR, Sharpe, and
maximum drawdown. Reported turnover is average one-way turnover across fills;
the initial entry is included.

This artifact is permanently `researchOnly: true` and
`decisionGrade: false`. It reconstructs history using today's SPX/NDX/DJIA
membership, creating survivorship bias, and its three displayed variants were
selected in-sample from a 40-variant sweep on overlapping history. Therefore
neither absolute returns nor relative strategy rankings are out-of-sample
evidence. Schema v2 records this methodology, and artifact validation rejects
older execution semantics or any artifact that claims decision-grade status.

## Market-Mass Research

`scripts/market_mass_boundaries.py` estimates a recency- and dollar-volume-
weighted center of accepted participation in split/distribution-adjusted
log-price space, then blends mass walls with realized/implied volatility to
create probabilistic boundary zones.

`scripts/market_mass_credit_spread_backtest.py` uses those zones to research
defined-risk credit spreads and iron condors. It is a model-priced research
tool, not a historical option-fill tape or live trade signal.

Market-mass state is point-in-time: every historical state sees only price and
volatility observations dated on or before that state. Implied-volatility
observations older than seven calendar days are excluded rather than blended
indefinitely, and missing dollar volume reduces center quality. Daily bars are
the only supported interval because volatility annualization and horizon units
assume trading sessions. YFinance inputs use adjusted OHLC; Stooq fallback
adjustment is explicitly marked unverified. Trade, mass, and volume-proxy
series may have different leading/trailing coverage, but an internal calendar
hole or duplicate date fails closed instead of silently deleting a session.

Boundary calibration uses the empirical target quantile of each historical
path's required asymmetric log-width. The older ratio of target coverage to
observed coverage was removed because coverage is not linear in band width.
Calibration remains in-sample and `decisionGrade: false`; the artifact reports
the uncapped multiplier, the 2.5x cap, and calibrated in-sample coverage.
Bands with fewer than 30 historical forecasts are reported but their
multiplier is not applied.

The credit-spread engine defaults to a one-completed-bar signal lag, next-Friday
expiry mapping with prior-session holiday adjustment, non-overlapping capital,
commission-inclusive sizing, and modeled entry/exit slippage. Expiry settlement
uses intrinsic value rather than a fabricated extra day of time value. A breach
stop is marked no better than the modeled short-strike touch value, even if the
daily close later reverses. Snapshot replay rejects malformed, crossed,
mid-only, stale, and post-entry quotes.

Synthetic Black-Scholes valuation includes an explicit constant dividend yield
and separate fixed put/call IV multipliers. At modeled exit closes the base
volatility is re-estimated from dated broad volatility and realized inputs when
available. These remain proxy assumptions, not a historical option surface;
exit quotes, security-specific pathwise skew, intraday event ordering,
assignment/margin, taxes, and market impact remain unobserved.
Drawdown is therefore realized trade-exit drawdown, not daily option
mark-to-market drawdown. Calendar walk-forward model selection is nested: each
test year selects its configuration only from the preceding training years.
The separate fixed holdout table remains ordered by training rank and is never
re-ranked to choose a holdout winner.

## AI Scoring and AICS Contract

The AI-SemiQuant and AI watchlist factor values are curated research priors on
a 0-100 scale. Producers reject missing, boolean, non-finite, negative, and
above-100 factor values instead of silently substituting a neutral score.
Curated priors do not have point-in-time fundamental histories or complete
row-level source lineage, so they are not decision-grade by themselves.

For non-USD primary listings, return, trend, volatility, drawdown, RSI, and beta
inputs are calculated from the adjusted local close converted through the
backward-looking daily FX series. Missing or stale FX blocks comparable market
analysis. Cross-market beta remains a non-synchronous-close approximation.
Current Yahoo profile market cap is timestamped; when it was fetched after the
dashboard decision date, it remains display-only and is excluded from
size/torque scoring.

The watchlist keeps the evidence-level prior and its effective numerical score
separate. A claim without a valid HTTP(S) source URL is capped at 64/100;
expired, future-dated, invalid-date, or refresh-required evidence is capped at
54/100 or lower. `verified: true` plus complete, current source metadata is
required before an evidence row can be marked decision-grade. The model card
also discloses the effective research-priority weights, including the deliberate
extra emphasis on proof points and underappreciation outside the structural
subscore. Data-quarantined rows are excluded from final/tactical percentiles.

AICS labels are proxies precisely:

- `industrialCapitalFlowScore` is a curated capex-conversion prior, not observed
  fund flow or orders;
- `financialCapitalFlowScore` is a price-momentum proxy, not securities flow;
- `valuationScore` is a curated valuation-growth prior, not a live multiple or
  DCF; and
- causal return attribution is unavailable, so earnings, multiple, flow, and
  FX/dividend contribution fields are withheld rather than fabricated.

The AICS static basket section is a descriptive, in-sample cross-section: the
current ranks are shown beside trailing returns that also informed tactical
scores. It is not a backtest. Saved-snapshot validation uses signal snapshot
`t`, enters only at snapshot `t+1`, and measures to `t+2`; it requires complete
USD price coverage, excludes hard data-review rows, and charges modeled costs
on first formation plus both sides of rebalances (including equal-weight drift).
Sharpe and Sortino use geometric daily-equivalent net returns. Sparse snapshot
drawdown cannot observe intraperiod losses. Saved point marks do not yet
independently reconcile dividends and corporate actions across runs, and there
is no historical spread, market-impact, borrow, or tax model, so AICS history
validation remains `decisionGrade: false` even after its sample-count gate.

## Memory Flow and Cross-Market Structure

`scripts/memory_flow.py` separates three evidence layers: observed public
flows, market-mechanics proxies, and behavioral hypotheses. KRX investor
categories, official KRX short transactions and threshold-reported balances,
KSD SEIBRO lending balances, KOFIA market-wide credit/deposit balances, FINRA
off-exchange short-sale volume, and daily OHLCV retain distinct labels. Short
transaction volume is never called short interest; borrowed inventory is never
called directional short exposure; and open interest without participant sign
cannot establish dealer gamma.

U.S. and Korean sessions use their own market timezone and close. Technical
confirmation requires at least 35 completed daily bars. ATR and RSI use Wilder
seeding; a flat RSI is 50. Missing EMA/slope/regime inputs block the regime
instead of being coerced to zero. A live partial Korean daily bar cannot confirm
a repair.

Share-based percentages require a positive denominator, an as-of date, and an
HTTP(S) source. If a corporate-action validity boundary is configured, ratios
after that date are withheld. Issued shares and outstanding shares are separate
fields: offering dilution uses verified pre-offering issued shares, while KRX
balance and lending percentages use verified outstanding shares. Samsung
reference estimates stay display-only until officially sourced.

Foreign-ownership changes measure full session intervals: a five-session change
requires six observations and a 20-session change requires 21. Incomplete
windows are withheld with sample metadata. Missing KOFIA forced-liquidation data
remains null with a quality status; it is never interpreted as zero.

SK hynix ADR parity requires a sourced ADS conversion ratio and synchronized
local/ADR/FX observations. Without the verified ratio it publishes no parity;
before regular trading, the reported offer price is labeled a secondary-source
anchor rather than a traded price. Deposit/cancellation restrictions and
non-synchronous market closes mean even a displayed premium is not guaranteed
executable arbitrage.

The memory-flow artifact is intentionally `decisionGrade: false`. Public data
does not provide SK hynix-specific margin credit, leveraged-ETF creations and
redemptions, signed dealer inventory, or complete U.S. retail flow. Its action
labels are hypothesis-review gates, not autonomous trade instructions.

## Artifact and Decision-Grade Contract

Optional research producers publish versioned, strict JSON by atomic replacement.
The dashboard validates each schema, rejects non-finite values and unsupported
versions, compares its as-of date with the portfolio price date, and shows
missing/stale/provisional status in the UI. Presentation compaction removes
unused/private provenance and large duplicate series before embedding.

Freshness is not the same as validation. Synthetic option pricing, current-
constituent momentum backtests, insufficient AICS history, current-fundamental
historical overrides, and other explicitly modeled research remain
`decisionGrade: false` even when their input file is current. They can inform a
hypothesis but must not be presented as executable evidence.

## Privacy Rules

Do not commit:

- real broker exports
- generated dashboards
- sync logs
- price caches tied to a private universe
- financial API caches
- screenshots of private dashboards
- Telegram or API credentials

Use `examples/` for synthetic fixtures and `output/` for local runtime artifacts.
