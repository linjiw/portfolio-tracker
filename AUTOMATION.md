# intraday-tape-reading Automation

This file is the wiring contract for the Codex-driven QQQ intraday tape loop.
`SKILL.md` is the brain; this document and the scripts below are the body.

The live pipeline is:

```text
launchd every 15 minutes during regular market hours
  -> python3 scripts/intraday_tape_sensor.py --symbol QQQ
  -> L4a hard gates from output/intraday_tape/gates.json
  -> read output/intraday_tape/state.json
  -> read output/intraday_tape/observation.json
  -> read output/intraday_tape/positions.json when present
  -> Codex Judge applies intraday tape + market sentiment + QQQ teacher skills
  -> python3 scripts/intraday_tape_finalize.py
  -> Telegram sendMessage
```

The Python layer is deterministic. It may pull data, compute levels, update
test counts, and enforce hard gates. It must not override the hard gates with a
trade opinion. Codex is the Judge layer only.

## Runtime Files

- `output/intraday_tape/observation.json`: closed-bar tape observation.
- `output/intraday_tape/gates.json`: hard gates that Codex may not override.
- `output/intraday_tape/state.json`: persistent levels, scenarios, events,
  verdict history, burden of proof, and last dispatch hash.
- `output/intraday_tape/positions.json`: optional manual positions overlay.
- `output/intraday_tape/telegram_message.html`: Judge output to dispatch.

## Hard Gates

- G1 time gate: before 09:45 ET or after 14:30 ET locks new entries.
- G2 event gate: T-60m to T+15m around state events locks new entries and
  requires the event protocol.
- G3 vacuum gate: 15m rvol < 1.0 and cum_rvol < 1.0 caps score at WATCH and
  prohibits ALLOW.
- G4 cross-event DTE gate: Judge must halve or reject spread ideas crossing the
  next binary event.
- G5 chase gate: price > 1.5x ATR(20) from the 20-bar mean blocks same-direction
  chase entries.

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

## Current Codex Automation

Name: `org.portfolio.tracker.intraday-tape-codex`

Schedule: launchd `StartInterval = 900` seconds, regular market window
09:30-16:05 ET on weekdays.

The prompt is self-contained: it runs the sensor, reads JSON inputs and skills,
writes the Telegram HTML message, finalizes state, sends Telegram, and reports
only a compact status line in the runner log.
