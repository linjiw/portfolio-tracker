# Portfolio Automation

## Locked Close Refresh

`scripts/refresh_portfolio_intelligence.py` is the single owner of the daily
close pipeline. It takes a non-blocking process lock and executes this order:

```text
broker-exact sync to a staging dashboard
  -> independent equity/cash/options/account verification gate
  -> initial current-price render
  -> financial + AI-SemiQuant + AI watchlist + AICS + momentum
  -> market-mass + USD liquidity + decision analysis
  -> final cache-only current-price render
  -> market sentinel -> SPMO -> position signals -> trend plan -> daily brief
```

Every step, command, return code, timing, input SHA-256, and output summary is
recorded under `output/refresh_runs/<run-id>/manifest.json`. Intermediate
broker/current-price dashboards remain private staging inputs; the canonical
dashboard is replaced atomically only after the final cache-only price render
passes its broker-fingerprint and price-freshness gates. A partial producer
failure is visible as
`complete_with_warnings`; broker or final-price gate failures stop publication.

The weekday close installer now points the 13:25 local job at this orchestrator
instead of letting the daily brief perform another independent sync:

```bash
python3 scripts/install_daily_brief_launchd.py
```

Do not install separate near-close price and brief jobs at overlapping times;
the orchestrator owns that dependency chain. Intraday monitoring remains
separate because its cadence and closed-bar gates are different.

## Intraday Tape Reading

This file is the wiring contract for the Codex-driven QQQ intraday tape loop.
`SKILL.md` is the brain; this document and the scripts below are the body.

The live pipeline is:

```text
launchd every 15 minutes during regular market hours
  -> trusted runner executes python3 scripts/intraday_tape_sensor.py --symbol QQQ
  -> L4a hard gates from output/intraday_tape/gates.json
  -> read output/intraday_tape/state.json
  -> read output/intraday_tape/observation.json
  -> read output/intraday_tape/positions.json when present
  -> offline/sandboxed Codex Judge applies tape + sentiment + teacher skills
  -> trusted runner validates Judge outputs and publishes them atomically
  -> trusted runner executes python3 scripts/intraday_tape_finalize.py
  -> Telegram sendMessage
```

The Python layer is deterministic. It may pull data, compute levels, update
test counts, and enforce hard gates. It must not override the hard gates with a
trade opinion. Codex is the Judge layer only.

The standalone `monitor_qqq_intraday.py` path uses this same event/time/volume/
freshness gate engine. It therefore also fails to `BLOCK_DATA` when the event
calendar is missing or expired and caps all candidate structures when G1/G2/G3
or G5 is active; running a second monitor cannot bypass the Codex-tape gates.
Its `ALLOW` label is explicitly tape-only (`tradeActionAuthorized: false`):
portfolio, sizing, liquidity, exact-expiry and max-loss gates still belong to
Market Sentinel/Codex Judge and no standalone monitor output authorizes a trade.

## Runtime Files

- `output/intraday_tape/observation.json`: closed-bar tape observation.
- `output/intraday_tape/gates.json`: hard gates that Codex may not override.
- `output/intraday_tape/state.json`: persistent levels, scenarios, events,
  verdict history, burden of proof, and last dispatch hash.
- `output/intraday_tape/positions.json`: optional manual positions overlay.
- `output/intraday_tape/events.json`: required validity-bounded event calendar.
- `output/intraday_tape/telegram_message.html`: Judge output to dispatch.

The event calendar is fail-closed. Missing, invalid, or expired data blocks new
entries. An explicitly valid empty list is allowed:

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-07-09T06:00:00-04:00",
  "validThrough": "2026-07-10",
  "events": [
    {"t": "2026-07-09T10:00:00-04:00", "name": "Economic release"}
  ]
}
```

Every event timestamp must include a UTC offset. `validThrough` is an inclusive
New York calendar date and should be refreshed alongside the daily event plan.

## Hard Gates

- G0 market-data gate: missing, stale, future, or prior-session closed 5m/15m
  bars produces `BLOCK_DATA` and locks new entries. Freshness is measured from
  bar close; one interval plus five minutes is the maximum age, so a fully
  missing closed bar fails rather than being silently reused.
- G1 session/time gate: weekends and standard US equity holidays lock new
  entries. On open sessions the window begins 15 minutes after the open and
  ends 90 minutes before the scheduled close (09:45-14:30 ET normally;
  09:45-11:30 on a 13:00 early close). The built-in calendar covers recurring
  holidays/early closes; unscheduled closures must also be entered in the
  required event calendar.
- G2_DATA calendar gate: missing, invalid, or expired `events.json` produces
  `BLOCK_DATA` and locks new entries.
- G2 event gate: T-60m to T+15m around validated calendar events locks new entries and
  requires the event protocol. For an off-quarter-hour release, the lock stays
  active beyond T+15 until a 15-minute candle that starts after the event has
  fully closed; a mostly pre-event bar is not confirmation.
- G3_DATA volume-data gate: fewer than three prior sessions at the exact same
  closed 15-minute slot (or a missing cumulative baseline) caps the result at
  WATCH and prohibits ALLOW. Missing volume evidence never counts as a pass.
- G3 vacuum gate: 15m rvol < 1.0 and cum_rvol < 1.0 caps score at WATCH and
  prohibits ALLOW.
- G4 cross-event DTE gate: Judge must halve or reject spread ideas crossing the
  next binary event, and must reject an expiry beyond the event calendar's
  `validThrough` coverage. An empty event list does not imply coverage beyond
  that date.
- G5 chase gate: absolute price extension > 1.5x ATR(20) from the 20-bar mean blocks same-direction
  chase entries.

G3/G3_DATA/G5 are evaluated only after G0 freshness passes; a stale prior-day
bar cannot be mislabeled as a current vacuum or chase condition.

Intraday rvol compares the closed bar with the same time-of-day bar in prior
sessions; cumulative rvol compares volume accumulated through that same slot.
ATR(20) and daily ATR(14) use Wilder true-range smoothing after a full warmup.
The daily weather candle is excluded until 16:15 ET so a forming daily bar
cannot move EMA/ATR/RSI gates. Daily historical OHLC is split/distribution
adjusted onto the current price scale. Intraday history uses the same basis;
the current session's factor is 1, so current bars remain on the executable
price scale. A flat-price Wilder RSI is neutral (50), not 100.

## Message Quality Contract

The Telegram message must look like a Codex audit, not a sensor heartbeat.
During market hours, every regular 15-minute loop includes:

- verdict and change versus prior loop
- 5m / 15m / 30m structure read
- upper/lower battlefield levels with confluence and test count
- 15m rvol and cumulative rvol verdict
- surviving scenario, trigger, and invalidation
- existing position management when `positions.json` is non-empty
- hard gates and the one allowed next behavior

Hash dedupe is retained as metadata, but unchanged verdicts are no longer
compressed to a one-line heartbeat by default.

Each Judge diff must echo the sensor `run_id`. The trusted runner and finalizer
both reject stale/mismatched diffs, verdict/message disagreement, and any
`ALLOW` that conflicts with `prohibit_allow`, `action_lock`, or a `BLOCK_DATA`
score cap. Concurrent sensor, monitor, Judge, and dispatch runs use process
locks; manual sends write a separate `manual_dispatch_log.csv` so they cannot
corrupt the automated dispatch log schema.

## Current Codex Automation

Name: `org.portfolio.tracker.intraday-tape-codex`

Schedule: launchd `StartInterval = 900` seconds, session-calendar market window
09:30 through scheduled close + 5 minutes (16:05 ET normally, 13:05 on a
13:00 early close); standard holidays/weekends are skipped.

The Python runner owns sensor and finalizer network access. The unattended
Codex process is ephemeral, receives a minimal environment with no inherited
trading/API credentials, has network access disabled, and can write only to a
private Judge staging directory. It cannot bypass approvals or the sandbox.
The runner requires fresh, regular, schema-complete Judge outputs before moving
them into the runtime directory and sending Telegram. The shell entry point
delegates to this same Python runner.

## Momentum Price Cache

`scripts/momentum_top3.py` treats `output/momentum_prices.csv.gz` as a
last-known-good dataset. A live refresh must contain both current benchmarks,
the configured history window, the minimum universe size, and configured
download/fresh-symbol coverage. Candidates are written to a private temporary
gzip file, read back and revalidated, then atomically replaced under a process
lock. Partial or corrupt downloads do not overwrite the prior validated cache;
the JSON artifact discloses the fallback in `universe_source` and
`universe_warning` and declares `schemaVersion: 1`.
