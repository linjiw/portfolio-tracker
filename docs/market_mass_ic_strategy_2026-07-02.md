# Market Mass IC Strategy Log For 2026-07-02

Generated: 2026-06-26 after the daily bar. Research only; not financial advice.

## Purpose

Rerun the weekly iron-condor strategy for the Thursday, 2026-07-02
expiration using the new discounted `swing` gravity profile. Friday,
2026-07-03 is a market holiday, so July 2 is the practical weekly expiration.

The key question was whether the older center-of-mass was too low for a
one-week option strategy. The experiment confirms that a slow structural
center is too stale for weekly strike selection. The current preferred weekly
profile is:

- `swing`: 84-bar lookback, 21-bar half-life.
- Purpose: weekly options and short-premium filtering.
- Role of structural center: long-horizon context only.

## Commands Rerun

Boundary diagnostics:

```bash
python3 scripts/market_mass_boundaries.py \
  --price-ticker '^NDX' \
  --volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --interval 1d \
  --gravity-profile swing \
  --horizons 4,5,10,21 \
  --confidences 0.68,0.75,0.80,0.95 \
  --out-json output/market_mass_boundaries/ndx_swing_2026-07-02_plan_summary.json \
  --out-csv output/market_mass_boundaries/ndx_swing_2026-07-02_plan_boundaries.csv \
  --out-md output/market_mass_boundaries/ndx_swing_2026-07-02_plan_report.md
```

Weekly IC backtest:

```bash
python3 scripts/market_mass_credit_spread_backtest.py \
  --price-ticker QQQ \
  --mass-price-ticker '^NDX' \
  --mass-volume-ticker QQQ \
  --vol-ticker '^VXN' \
  --fallback-vol-ticker '^VIX' \
  --period 5y \
  --interval 1d \
  --gravity-profile swing \
  --side-policy iron_condor \
  --confidence 0.75 \
  --quality-min 55 \
  --max-build-up 45 \
  --width 10 \
  --min-credit-risk 0.12 \
  --min-credit 0.05 \
  --max-short-delta 0.35 \
  --blocked-ic-weather overheat \
  --profit-take-pct 0.55 \
  --stop-loss-multiple 2.20 \
  --out-dir output/options_backtest/qqq_ndx_mass_ic_swing_2026_07_02_plan
```

## Current Center Of Mass

As of the 2026-06-26 daily bar:

| Item | NDX gravity | QQQ direct |
|---|---:|---:|
| Price | 29,118.24 | 706.52 |
| Center of mass | 28,533.50 | 694.35 |
| Quality | 81.3 | 81.3 |
| Regime | active_center | active_center |
| Distance-z | +0.286 | +0.244 |
| Mass sigma | 7.35% | 7.37% |
| Center weight | 0.85 | 0.85 |
| Build-up score | 19.4 | 19.5 |
| Annual vol used | 31.0% | 31.2% |
| VXN/VIX ratio | 1.636 | 1.636 |

Interpretation:

- The market has a real usable center under the weekly `swing` profile.
- Price is slightly above the center but not detached.
- Build-up is low enough for defined-risk premium research.
- The issue is not gravity quality. The issue is entry regime and option
  economics.

## QQQ Weather Gate

QQQ as of 2026-06-26:

- Close: 706.52
- EMA8: 718.16
- EMA13: 720.06
- EMA21: 718.89
- EMA34: 711.00
- EMA55: 695.42
- ATR14: 19.47
- 5-day return: -4.60%
- EMA21 5-day slope: -1.82
- Weather: `break`

Interpretation:

- This blocks a blind Friday-close iron condor entry.
- A break regime does not automatically mean a crash, but it raises short-gamma
  risk and requires a better entry trigger.
- For July 2, the correct decision label is `WATCH/BLOCK`, not `ALLOW`.

## Four-Day Boundary Zones Into 2026-07-02

NDX gravity with QQQ volume proxy:

| Confidence | Lower zone | Boundary | Upper zone |
|---:|---:|---:|---:|
| 68% | 26,694 - 26,902 | 26,797.96 | 30,448 - 30,686 |
| 75% | 26,404 - 26,642 | 26,522.98 | 30,745 - 31,023 |
| 80% | 26,162 - 26,426 | 26,293.74 | 30,998 - 31,309 |
| 95% | 24,948 - 25,333 | 25,139.62 | 32,335 - 32,834 |

QQQ direct equivalent:

| Confidence | Lower zone | Boundary | Upper zone |
|---:|---:|---:|---:|
| 68% | 649.18 - 654.28 | 651.72 | 740.72 - 746.54 |
| 75% | 642.11 - 647.94 | 645.02 | 747.96 - 754.76 |
| 80% | 636.22 - 642.66 | 639.43 | 754.11 - 761.75 |
| 95% | 606.60 - 616.03 | 611.30 | 786.72 - 798.93 |

## Backtest Result

The updated swing-profile IC model, trading QQQ options with `^NDX` gravity,
returned:

- Trades: 35
- Win rate: 88.6%
- Model P&L: +$2,088.20 on $100,000 starting capital
- Return: +2.09%
- Max drawdown: -0.57%
- Profit factor: 2.85
- Short-strike touch rate: 31.4%
- Average absolute short delta: 0.253

This is better than the stale structural-center version, but it is still a
synthetic model backtest. It estimates option premiums with Black-Scholes and
does not replay historical point-in-time option chains.

## Current Option Chain Check

Snapshot: 2026-06-26T23:06Z from `yfinance`.

Model-pure QQQ boundary IC candidates:

| Candidate | Credit | Max risk | Credit/risk | Short deltas | Read |
|---|---:|---:|---:|---:|---|
| 645/635P + 755/765C | $0.14 | $9.86 | 0.014 | -0.017 / +0.004 | Too little credit |
| 640/630P + 760/770C | $0.09 | $9.91 | 0.009 | -0.014 / +0.002 | Too little credit |

Closer QQQ income variants:

| Candidate | Credit | Max risk | Credit/risk | Short deltas | Read |
|---|---:|---:|---:|---:|---|
| 680/670P + 730/740C | $2.47 | $7.53 | 0.328 | -0.133 / +0.130 | Inside boundary; higher touch risk |
| 670/660P + 735/745C | $1.46 | $8.54 | 0.171 | -0.073 / +0.078 | Inside boundary; still not model-pure |
| 660/650P + 740/750C | $0.79 | $9.21 | 0.086 | -0.040 / +0.041 | Lower credit and still inside boundary |
| 650/640P + 745/755C | $0.38 | $9.62 | 0.040 | -0.023 / +0.019 | Near boundary but poor credit |

Model-pure NDX boundary IC candidates:

| Candidate | Credit | Max risk | Credit/risk | Short deltas | Read |
|---|---:|---:|---:|---:|---|
| 26500/26400P + 30900/31000C | 0.0 pts | 100.0 pts | 0.000 | -0.016 / +0.007 | Too little quoted credit |
| 26500/26400P + 31000/31100C | 0.0 pts | 100.0 pts | 0.000 | -0.016 / +0.005 | Too little quoted credit |

Closer NDX income variants:

| Candidate | Credit | Max risk | Credit/risk | Short deltas | Read |
|---|---:|---:|---:|---:|---|
| 28000/27900P + 29900/30000C | 29.7 pts | 70.3 pts | 0.422 | -0.134 / +0.174 | Inside boundary; aggressive income variant |
| 27500/27400P + 30100/30200C | 17.0 pts | 83.0 pts | 0.205 | -0.065 / +0.111 | Inside boundary; still aggressive |

## Decision For 2026-07-02

Official decision: `BLOCK` current entry, `WATCH` for Monday repricing.

The model has a good center and low build-up, but two gates fail today:

1. QQQ weather is `break`, so short-gamma entries need confirmation.
2. Boundary-pure strikes do not pay enough credit at current quotes.

The official pick is therefore not to force an iron condor at Friday's quote.

Conditional model-pure QQQ pick if pricing improves:

- Expiration: 2026-07-02.
- Structure: sell 645P / buy 635P, sell 755C / buy 765C.
- Entry requirement: net credit should be at least about $1.05 for a 10-wide IC
  to meet the 0.12 credit/risk gate.
- Current quote: about $0.14, so no entry.

Conditional model-pure NDX pick if pricing improves:

- Expiration: 2026-07-02.
- Structure: sell 26500P / buy 26400P, sell 30900C / buy 31000C.
- Entry requirement: net credit should be at least about 10.7 index points for a
  100-wide IC to meet the 0.12 credit/risk gate.
- Current conservative bid/ask quote: about 0.0 points, so no entry.

Aggressive paper-only income variant:

- QQQ: 670/660P + 735/745C for about $1.46 credit.
- This meets credit/risk, but both short strikes are inside the model boundary.
- It should be treated as a research/paper candidate only while QQQ weather is
  `break`.

## Entry Rules For Monday 2026-06-29

Recheck before any order:

- QQQ must stabilize or reclaim EMA34/EMA21 area; otherwise keep `BLOCK`.
- VXN/VIX should not expand above roughly 1.75.
- VXN should not spike materially above the current 30.91 area.
- Use model-pure strikes only if credit/risk reaches at least 0.12.
- If using an inside-boundary income variant, size must be tiny/paper-only and
  explicitly accept higher touch risk.

## Risk Rules

For any live-defined-risk IC:

- Max loss is width minus credit, times 100 multiplier.
- Take profit at 50% to 60% of credit captured.
- Stop if the IC mark reaches about 2.0x to 2.2x entry credit.
- Reduce or close if a short strike is touched and price does not reject
  quickly.
- Do not hold through a confirmed regime change: QQQ failing EMA55 with rising
  VXN, or NDX/QQQ breaking below the lower volatility boundary with expanding
  build-up.

## Algorithm Meaning

The `swing` center answers a different question from the structural center:

- Structural center: where long-horizon accepted participation sits.
- Swing center: where recent participation has enough mass for a weekly option
  trade.

The older center looked too low because it remembered too much of the past bull
move. The fix is not to discard history entirely, but to discount it more
aggressively for short-dated trades. The current 84/21 setting behaves like a
short-horizon discount factor: recent bars matter more, but one or two sessions
cannot dominate the whole center.

For July 2, gravity says the market has an anchor. The option chain says the
safe outer walls are not paying enough. That is the decisive filter.
