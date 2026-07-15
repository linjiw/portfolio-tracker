# Semiconductor Leverage Tracker Methodology

This tracker measures market-wide leverage pressure in Korea and the United
States, then tests its relationship with Samsung Electronics, SK hynix, Micron,
and SOXX prices. The markets expose different official datasets, so their raw
ratios must not be compared directly.

## Data Contract

### Korea: Daily Official Balance

Source: Korea Financial Investment Association (KOFIA) FreeSIS.

- Margin-credit loans: `STATSCU0100000070BO`, total in `TMPV2`.
- Investor deposits: `STATSCU0100000060BO`, total in `TMPV2`.
- Units returned by KOFIA are KRW million.
- Primary ratio: total margin-credit loans / investor deposits.
- Supporting stress fields: brokerage receivables, forced-liquidation amount,
  forced-liquidation ratio, KOSPI credit, and KOSDAQ credit.

This is a daily Korean market aggregate. It is not the margin balance of
Samsung Electronics or SK hynix.

### United States: Monthly Official Balance

Source: FINRA Margin Statistics workbook.

- Debit balances in customer securities margin accounts.
- Free credit in customer cash accounts.
- Free credit in customer securities margin accounts.
- Primary ratio: debit balance / sum of both free-credit balances.
- Workbook units are USD million.

FINRA data are monthly and aggregate. Historical release timestamps are absent
from the workbook, so forward tests conservatively treat each observation as
available on the 25th of the following month.

### Micron: Daily Flow Proxy

Source: FINRA Reg SHO Daily Short Sale Volume API.

The tracker aggregates all reporting facilities by date, then calculates:

`sum(short volume) / sum(total reported volume)`

The displayed daily signal is a five-session mean. This series covers
off-exchange reported flow and is **not** leverage, short interest, borrowed
dollars, days-to-cover, or necessarily a bearish position.

### Prices

Adjusted daily closes come from Yahoo Finance through `yfinance`:

- Samsung Electronics: `005930.KS`
- SK hynix: `000660.KS`
- Micron: `MU`
- U.S. semiconductor benchmark: `SOXX`

These adjusted closes drive the return calculations and the comparison
timelines. The dashboard also fetches a separate latest-trade snapshot:

- Samsung Electronics and SK hynix: Naver Finance.
- Micron and SOXX: Nasdaq.

The quote snapshot includes currency, market session, source, and timestamp.
For U.S. symbols it also retains the regular-session close when Nasdaq reports
an extended-hours latest trade. A latest trade is not substituted into the
daily return history.

## Comparison Rule

Korea's percentage and the U.S. multiple have different definitions,
frequencies, account populations, and denominators. The cross-country panel
therefore compares only each market's own rolling z-score and percentile:

- Korea: up to 756 daily observations.
- United States: up to 36 monthly observations.

This is a pressure comparison, not a claim that the levels are equivalent.

## Quantitative Tests

The analysis uses changes in ratios rather than correlations between trending
levels.

1. Concurrent relationship: metric change versus the stock return between
   metric observations. This is descriptive and not tradeable.
2. Forward relationship: metric change versus 1/5/20-session returns for daily
   data and 1/3/6-month-equivalent returns for monthly data.
3. Reverse relationship: current stock return versus the next metric change,
   which helps expose price chasing and reverse causality.
4. Event study: average forward return after top- and bottom-quintile metric
   changes.
5. Uncertainty: deterministic moving-block bootstrap 95% intervals for Pearson
   correlations.

KOFIA and Reg SHO observations are aligned to the first price close on or after
the source date plus one calendar day. FINRA monthly observations use the
conservative estimated availability date above. Multi-session forward returns
overlap, so the output avoids ordinary independent-observation p-values.

## Runtime

```bash
python3 scripts/semi_leverage_tracker.py
python3 generate.py --no-fetch
```

Use cached inputs without network access:

```bash
python3 scripts/semi_leverage_tracker.py --no-fetch
```

Generated JSON, Markdown, and normalized caches are written under `output/` and
remain local.

## Limits

- Correlation does not establish causation.
- Market-wide leverage cannot identify stock-specific borrowed exposure.
- Price increases may attract credit, credit may amplify price moves, or both
  may react to liquidity, earnings, rates, or the memory cycle.
- Free public feeds can revise history or have exchange-calendar differences.
- This is research software, not investment advice or an order recommendation.
