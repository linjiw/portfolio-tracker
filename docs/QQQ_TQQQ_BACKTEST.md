# QQQ/TQQQ Close-Only Backtest Methodology

## Status

This backtest is **research-only and not decision-grade**. It is useful for
checking the internal behavior of the QQQ regime rules and TQQQ tactical sizing.
It is not evidence that a configuration will work in live trading.

Machine-readable status and limitations are emitted to `methodology.json` with
`decision_grade: false`.

## Data contract

- Inputs are overlapping, positive, finite QQQ and TQQQ adjusted daily closes.
- `--start` and `--end` are inclusive. The yfinance fetch path compensates for
  yfinance's exclusive end-date convention.
- Invalid dates, non-numeric closes, non-finite values, and non-positive prices
  fail closed instead of being silently skipped.
- QQQ and TQQQ must have identical session coverage inside the requested range;
  a one-sided missing row fails closed instead of being mistaken for a holiday.
  A date missing from both series is still undetectable without a market calendar.
- EMA values are seeded at the first requested observation. `--warmup` blocks
  tactical entries for that many rows; it does not supply pre-start history.
- ATR is a close-to-close absolute-change proxy because OHLC is unavailable.
- RSI is `null` until the full lookback exists. Early rows are not backfilled
  with a later RSI value.

## Signal and execution timing

The simulation follows this order for each trading row:

1. Execute an order queued by the prior session's close signal.
2. Mark holdings at the current adjusted close.
3. Calculate the current close-based regime and queue any new order.
4. Fill that new order only at the next available trading close.

The final row cannot fill a new signal. A close-based stop or trend-break is
therefore a next-close exit proxy and can experience a full session of gap and
path risk. It is not an intraday stop simulation.

Default equity fills use 5 bps of adverse slippage per side:

- buy fill = reference close × (1 + 0.0005)
- sell fill = reference close × (1 - 0.0005)

The default fixed commission is $0 per order, but the parameter is explicit and
all modeled commissions and slippage reduce cash/P&L. The QQQ/TQQQ buy-and-hold
and daily-DCA comparison curves use the same default buy slippage.

The DCA comparator is a fixed-horizon benchmark: it divides initial capital by
the number of observed rows and invests the full amount by the selected end
date. It therefore knows the test horizon and is not a live calendar-year DCA
plan when the end date moves forward each day.

## Return and risk math

- Total return is ending marked equity divided by initial capital minus one.
- Annualized return is geometric CAGR using an ACT/365.2425 calendar-day basis
  between the first and last close. It can exaggerate short samples, so period
  total return remains the primary measure.
- Max drawdown is peak-to-trough loss with initial capital inserted as the first
  peak. This captures inception execution cost and an immediate first-row loss,
  but daily closes can still understate intraday drawdown.
- Open tactical positions are marked at the final close; no hypothetical final
  liquidation cost is deducted.
- A partial exit and the remaining final mark are attributed to one tactical
  position rather than counted as separate entries.

## CCS diagnostic

The CCS module has no historical option chain. Its credit formula, strike map,
and expiration payoff are synthetic scenario inputs. It therefore:

- observes an overheat signal at close *t*;
- schedules the scenario entry for close *t+1*;
- keeps only complete holding horizons (no shortened end-of-data trades);
- subtracts a default $4.60 assumed execution cost per spread;
- reports conditional max gain and max loss from the assumed credit and $1
  spread width, with those same execution costs included;
- labels every row `decision_grade: false`; and
- excludes the payoff from strategy final value, return, drawdown, and sweep
  ranking.

The diagnostic does not model historical bid/ask, IV/skew, Greeks, early
assignment, dividends, exercise fees, or strike availability. Its values must
not be described as historical option P&L.

## Parameter sweep

Every sweep is in-sample. “Highest” or “best” refers only to the selected sample
and is selection-biased. No configuration is validated until it passes a
pre-registered walk-forward or out-of-sample test.

## Requirements before decision use

At minimum, a decision-grade successor needs:

- split/dividend-verified OHLC data with a documented market calendar;
- next-open or executable quote-based fills and sensitivity across cost levels;
- historical option quotes/chains with bid/ask, IV/skew, volume/open interest,
  corporate actions, and assignment handling;
- cash yield, taxes where relevant, and realistic market-impact assumptions;
- pre-registered train/validation/test windows and walk-forward evaluation;
- robustness checks across regimes, start dates, and TQQQ corporate actions;
- confidence intervals and multiple-testing controls for parameter searches.

## Verification

Run the focused suite with:

```bash
python3 -m pytest -q tests/test_qqq_tqqq_backtest.py
```
