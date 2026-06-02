---
name: portfolio-sync
description: Sync the portfolio tracker/dashboard from fresh Fidelity exports. Use whenever the user provides a new Portfolio_Positions_*.csv and/or Accounts_History*.csv (or History_for_Account*.csv), or asks to "sync", "update", or "refresh" their portfolio, holdings, tracker, dashboard, or website. The user drops new exports in ~/Downloads and expects Claude to read them and regenerate everything itself.
---

# Portfolio sync

The user periodically exports fresh CSVs from Fidelity into `~/Downloads` and
asks me to update the tracker. This dashboard project turns those exports into a
single self-contained `output/portfolio_dashboard.html` (the "tracker"/"website").

## Do this

```bash
cd /Users/linji/Downloads/portfolio-tracker
python3 sync.py            # full sync: regenerate + verify + log (fetches prices)
python3 sync.py --no-fetch # offline: reuse cached prices in output/prices_cache.json
python3 sync.py --open     # also open the dashboard when done
```

`sync.py` is the one command to run. It:
1. delegates to `generate.py` (the single source of truth) which auto-detects the
   **newest** `Portfolio_Positions*.csv` and **merges every** `Accounts_History*.csv`
   + `History_for_Account*.csv` in `~/Downloads`, fetches prices, writes the HTML;
2. **verifies** the written dashboard's market value / unrealized P&L / held count
   against an independent sum of the newest Portfolio CSV — fails loudly on any
   mismatch (this is the gate that catches a dropped account or holding);
3. **logs** a snapshot to `output/sync_log.json` and prints the delta vs last sync.

If `sync.py` exits non-zero on the verification step, the dashboard is WRONG —
investigate `generate.parse_portfolio` before trusting it (see below).

## Always confirm after a sync
- The verification block prints `OK` for marketValue, unrealized, numHeld.
- `accounts in snapshot:` lists **every** account in the export — brokerage
  (`Z...`) AND retirement (numeric, e.g. a Rollover IRA `257937289`). If an
  account you expect is missing, a holding is being dropped.
- Date window and held count look right; market value ≈ sum of the Portfolio CSV.

## Gotchas (why the code is the way it is — don't regress these)
- **Multiple accounts.** Sum each symbol across ALL accounts, brokerage and
  retirement. `parse_portfolio` matches account rows by an alphanumeric id in
  col 0 — never a `Z`-only prefix (that silently dropped the IRA once).
- **Money market is cash, not equity.** Skip any symbol ending `**`
  (`SPAXX**`, `FDRXX**`) — they are the cash core, counted separately.
- **History is incomplete / multi-export.** Exports overlap and some miss weeks;
  `generate.py` merges them (dedup by max-count per identical trade) and anchors
  the timeline to the largest gap-free span ending at the last trade. Holdings
  are reverse-reconstructed from the Portfolio CSV, so legacy/exited names are
  exact. Full methodology + the two P&L definitions live in `../../SKILLS.md`.
- **New tickers** are derived from the CSVs at runtime; a full sync fetches them
  automatically. If `generate.py` prints `missing: [...]`, that symbol got no
  price line (delisted/renamed) — note it, it's usually acceptable.
- **Offline?** Use `--no-fetch` (reuses the cache; the tail of the window may be
  stale). Network needs Yahoo Finance via `yfinance`.

## After regenerating
Tell the user the headline numbers from the sync output (market value,
unrealized, TWR vs S&P/NASDAQ, what changed since last sync). Only commit if the
user asks. Raw `*.csv` exports are gitignored on purpose (personal data); the
dashboard HTML and `sync_log.json` are tracked.
