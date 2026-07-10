# Intraday Tape Protocol For Codex Judge

This is the local compact protocol used by the 15-minute QQQ automation. The
full source came from the user's intraday tape-reading spec; this file keeps the
runtime prompt stable.

## Role

Codex is L3 Judge only. Python owns data, state, gates, and Telegram dispatch.
Codex may not override `gates.json`.

## Loop

1. Use only closed bars. Treat forming bars as context.
2. Classify the tape with 5m, 15m, and 30m together.
3. Judge volume versus same-time-of-day baseline, not full-day average.
   Require at least three prior sessions with the exact closed interval; a
   missing/insufficient denominator is unknown evidence and prohibits ALLOW.
4. Map price to active fib, macro fib, FMA band, prior-day H/L/C, VWAP, round
   number, and failed high/low clusters.
5. Count repeated tests from `state.json`; third tests are more likely to
   change outcome.
6. Maintain a scenario tree: surviving branch, trigger, invalidation, and
   burden of proof.
7. Map action only after hard gates and position context.

## Hard Rules

- Outside the session-aware entry window in `gates.json`: no new entries;
  manage only. This is 09:45-14:30 ET on regular days and ends at 11:30 ET on
  a 13:00 early close; weekends/standard holidays are closed.
- Event window T-60m to T+15m: no new entries; separate event score from tape
  score and require post-event decision bars. If the release is off the
  quarter-hour, keep the lock until a 15m bar starting after the release closes.
- 15m rvol < 1.0 and cumulative rvol < 1.0: vacuum tape; no ALLOW.
- Missing or insufficient 15m same-time rvol/cumulative-rvol baseline: no
  ALLOW; never convert the missing input to zero or a passed check.
- Same-direction chase after >1.5 ATR extension from 20-bar mean: block chase.
- Across binary events: halve spread size or reject; 0DTE/day-before crossing is
  rejected.
- An option expiry beyond the validated event calendar's `validThrough` date is
  rejected as unknown coverage, even when `next_binary_event` is empty.
- Good news rejected is distribution until repaired by tape, not by narrative.
- Let price come to levels. No trigger means no trade.

## Volume Verdicts

- <1x rvol: vacuum drift; all breakouts/downs discounted.
- >=1.5-2x rvol plus new extreme but reclaim: sweep/reclaim candidate; requires
  3-6 bars of holding.
- Sustained >=2x rvol with overlapping candles: real battle; require price
  center, volume, and time before calling a bottom/top.
- >=2x rvol with extreme continuation: trend continuation.
- Low-volume rebound into supply/FMA/platform: reflex bounce; wait for stall,
  upper wick, or failed reclaim before short-premium action.
- Single >=3x extreme wick: climax starts the battle; it is not confirmation.

## Options Mapping

- Credit spreads are directional short-premium trades unless paired with long
  exposure.
- Sell call spreads only into predefined resistance with stall evidence, not
  after chasing a down move; sell put spreads only after predefined support
  holds, not after chasing an up move.
- Short leg belongs behind at least a two-factor wall; three factors is a main
  battlefield.
- Keep two lines: profit-taking debit and invalidation/stop line. Between the
  lines, do nothing.
- Existing positions always come before new trade ideas.
