# AI-SemiQuant Reference

This is the persistent reference document for the AI semiconductor analysis
framework requested in the goal objective. The generated run artifact lives at:

```text
output/ai_semi_quant_report.md
output/ai_semi_quant.json
```

Regenerate the live framework and dashboard tab:

```bash
python3 scripts/ai_semi_quant.py
python3 generate.py --no-fetch
```

## Purpose

AI-SemiQuant is not a one-day price prediction model. It is a structured way to
answer five recurring investment questions:

- Who controls the scarce bottleneck in the AI semiconductor chain?
- Where does hyperscaler capex convert into company revenue and margin?
- Which names have true pricing power versus downstream volume exposure?
- Which companies are structurally strong but tactically stretched or broken?
- How much of the current portfolio already overlaps the same AI/semi factor?

## Capital Waterfall

The framework tracks capital through seven layers:

1. Hyperscaler capex: Microsoft, Google, Amazon, Meta, Oracle and other cloud
   buyers fund AI clusters.
2. Design/platform capture: NVIDIA, Broadcom and AMD turn those budgets into
   GPUs, ASICs, networking and platform revenue.
3. Advanced manufacturing: TSMC, Samsung Foundry and Intel Foundry compete for
   advanced-node wafer and packaging allocation.
4. HBM/memory: SK hynix, Micron and Samsung monetize the memory wall and HBM
   scarcity.
5. Equipment/materials: ASML, Applied Materials, Lam Research, KLA, ASM
   International, Besi and Hanmi convert fab capex into order backlog.
6. Advanced-packaging overflow: Amkor and ASE benefit when integrated CoWoS
   capacity is tight and OSAT partners absorb overflow.
7. AI server integration: Hon Hai, Quanta and Wiwynn monetize rack/server
   assembly after silicon is allocated.

## Structural Score

The structural Alpha Score is a weighted score from 0 to 100:

| Factor | Weight | Question |
| --- | ---: | --- |
| Pricing Power | 30% | Can the company raise price or ration scarce capacity? |
| Profit Elasticity | 24% | Does incremental revenue drop quickly to profit after utilization rises? |
| Capex Conversion | 24% | Does customer/fab capex become this company's backlog and revenue? |
| Valuation/Growth | 22% | Is expected growth still reasonable versus valuation and expectations? |

Size/Growth Torque is a separate overlay, not a fifth structural factor. It
exists because the same business growth rate can produce very different equity
outcomes at different starting market caps. A $15B company with a real
bottleneck position can compound or re-rate faster than a $2T company, but a
small cap without pricing power or capex conversion should not be rewarded just
for being small.

The factor is therefore computed as:

```text
Size/Growth Torque =
  50% curated growth quality
+ 35% market-cap elasticity bucket
+ 15% business-quality guardrail
```

Market-cap elasticity uses USD-normalized market cap when Yahoo provides it.
The score peaks in the small / small-mid buckets, then tapers through large,
mega, and hyper-scale companies. Very small names with weak business quality are
penalized rather than boosted.

The dashboard also adds a smaller tactical overlay from current price data:
trend score, risk score, and a penalty for overextension or volatility. This
keeps "great business" separate from "good entry."

## v0.3 Reliability And Calibration Layer

The system now separates company-level research from security-level market data:

- Company scoring uses `company_id`, structural factors, risk flags and the
  capital-flow map.
- Security data carries local price, trading currency, USD-converted price,
  market cap, FX source and display-safe price strings.
- ADR/local listings are represented as aliases on the same company record so
  company fundamentals are not intentionally double-counted.

Each generated row includes:

```text
priceLocal
currency
priceUsd
displayPrice
marketCapUsd
dataQualityScore
gateReasons[]
riskBreakdown{}
standaloneScore
portfolioAdjustedScore
structuralBaseScore
torqueAdjustedScore
universePercentile
peerPercentile
peerGroupSize
peerPercentileDisplay
```

Momentum is peer-relative rather than a simple absolute trend score. The
tactical score combines 3M/6M/1Y relative strength, trend regime and drawdown
resilience, then subtracts overextension penalties for extreme distance above
moving averages, RSI and unusually large 3M returns. This prevents the whole AI
semiconductor group from scoring 100 at the same time.

Data-quality issues do not become silent zero scores. Missing prices, missing
FX conversion, missing market cap, extreme 1D/5D returns, extreme 3M returns or
price bands far outside the trailing median reduce `dataQualityScore`; severe
anomalies produce `DATA_REVIEW` instead of a normal `BLOCK`. Soft anomalies stay
ranked but are listed by ticker, rule and reason in the model card.

v0.3 also separates structural quality from payoff convexity:

```text
structuralBaseScore = pricing power + profit elasticity + capex conversion + valuation/growth
torqueAdjustedScore = structuralBaseScore + size/growth torque bonus - fragility penalty
```

`sizeGrowthTorque` is an overlay, not a moat. It can help smaller high-quality
names, but customer concentration, volatility, and overextension can offset it.

## v0.3.1 Audit Cleanup

Peer percentiles are only user-facing when the peer group has enough breadth.
The raw `peerPercentile` remains in JSON for diagnostics, but the dashboard uses
`peerPercentileDisplay`:

```text
peerGroupSize < 3  ->  N/A · n=1 or N/A · n=2
peerGroupSize >= 3 ->  P83 · n=6
```

This prevents a single-company peer group such as TSMC's advanced
foundry/CoWoS bucket from looking like a statistically meaningful median rank.

`ALLOW_DD` explanations now state why the name is in due diligence instead of
an execution plan. A peer-confirmed case is phrased like:

```text
ALLOW_DD because strategic score 83 >= 82 and peer percentile P75 >= 70;
despite adjusted score 69 below WATCH threshold.
```

Small-sample or weak-peer cases say that explicitly. This keeps `ALLOW_DD` as a
research permission, not a buy signal.

The model card now exports:

```text
softDataFlags[]
hardDataFlags[]
```

Each flag carries ticker, company name, severity, rule and detail so the audit
panel can be inspected without opening the raw JSON.

## v0.4 Score Delta Attribution

v0.4 keeps the v0.3.1 scoring/gate framework intact and adds a dynamic monitor:
each run compares the current rows with the prior `output/ai_semi_quant.json`
snapshot before overwriting it.

Each row now includes:

```text
scoreDelta
scoreDeltaAttribution{
  previousFinalScore
  currentFinalScore
  finalDelta
  previousGate
  currentGate
  gateChanged
  bucketImpacts
  components[]
  drivers[]
  topDrivers[]
  addedDataFlags
  clearedDataFlags
  riskScoreDelta
  summary
}
```

The first attribution model is intentionally lightweight. It decomposes the
final-score change into the parts that directly drive the existing formula:

```text
Structure/torque contribution = Δ torqueAdjustedScore × 0.76
Tactical contribution         = Δ tacticalScore × 0.24
Risk penalty contribution     = -Δ riskPenalty
Portfolio penalty contribution= -Δ portfolioPenalty
```

The attribution is approximate because final scores are rounded and gates can
change for data-quality or threshold reasons. Those non-formula effects are
shown as gate/data-quality changes and, when needed, a rounding/other residual.

v0.4 slice 2 hardens the change taxonomy. Attribution drivers are grouped into
stable buckets:

```text
score_delta
gate_delta
strategic_delta
tactical_delta
risk_delta
portfolio_delta
data_quality_delta
```

The summary object now exposes `scoreDeltaTopChanges` so the dashboard can show:

```text
Top Score Increases
Top Score Decreases
Gate Changes
Risk Improvements / Deteriorations
Data Flags Added / Cleared
Portfolio Blocks Added / Removed
```

The model card also records prior/current run metadata:

```text
priorModelVersion
currentModelVersion
priorRunDate
currentRunDate
priorUniverseCount
currentUniverseCount
matchedTickerCount
newTickerCount
removedTickerCount
schemaCompatible
```

If the prior JSON is missing fields required for formula attribution, the system
sets `baselineReset=true` and rows use `status=baseline_reset` instead of
pretending to produce precise deltas.

This answers the first v0.4 question:

```text
分数为什么变了？
```

It does not yet claim that real capital has confirmed the move. Dynamic
capital-flow edge weights, live capex/holdings confirmation, and the first
lightweight backtest remain separate v0.4 follow-up modules.

## Research Gates

`ALLOW_PLAN` means the name can enter staged plan review. It is not an automatic
buy.

`ALLOW_DD` means the name deserves deeper diligence, but setup, risk or
portfolio constraints are not clean enough for a plan.

`WATCH` means the structure is attractive, but price, risk or timing requires a
cleaner trigger.

`WATCH_RESET` means the company remains strategically interesting but is too
overextended, risky, or technically damaged for new capital.

`PORTFOLIO_BLOCK` means the company may still be high quality, but current
portfolio concentration blocks additional adds.

`BLOCK` means the name is either below the framework threshold, technically
broken, or missing enough data to justify new capital.

`DATA_REVIEW` means the price/fundamental data needs review before the model
should rank the name. This is distinct from `BLOCK`: the company may be
interesting, but the data cannot be trusted yet.

Every gate must carry at least one reason, such as:

```text
risk_floor
trend_break
overextension
portfolio_concentration
score_below_threshold
daily_return_outlier
five_day_return_outlier
price_band_outlier
three_month_return_outlier
```

The dashboard shows the first reason in the main table and stores all reasons
in `output/ai_semi_quant.json`.

The dashboard also splits tactical rank into:

```text
Raw Tactical Rank         # pure tactical signal, can include blocked names
Investable Tactical Rank  # excludes hard BLOCK / PORTFOLIO_BLOCK / DATA_REVIEW
```

## Update Triggers

- Re-score OSAT and packaging equipment when CoWoS capacity, utilization or
  outsourcing data changes.
- Re-score HBM names after HBM share, contract-price, capex or customer
  qualification updates.
- Re-score equipment names after SEMI/WFE forecasts, order backlog or export
  controls change.
- Re-score foundry challengers only when external customer qualification,
  yield, utilization and losses move together.
- Always compare new AI/semi exposure against portfolio concentration and QQQ
  regime before treating a high score as actionable.

## Primary Source Anchors

- [TSMC annual reports](https://investor.tsmc.com/english/annual-reports)
- [TSMC fab capacity](https://www.tsmc.com/english/dedicatedFoundry/manufacturing/fab_capacity)
- [TrendForce CoWoS capacity coverage](https://www.trendforce.com/news/2026/06/15/news-tsmc-cowos-supply-demand-gap-reportedly-seen-narrowing-from-20-to-10-by-end-2026-as-capacity-expands/)
- [SEMI 300mm fab equipment outlook](https://www.semi.org/en/semi-press-release/semi-projects-double-digit-growth-in-global-300mm-fab-equipment-spending-for-2026-and-2027)
- [Counterpoint DRAM/HBM market share tracker](https://counterpointresearch.com/en/insights/global-dram-and-hbm-market-share)

## Boundary

This framework is decision support only. It does not replace position sizing,
tax review, stop/invalidation levels, options risk checks, or the existing
QQQ/TQQQ market gate.
