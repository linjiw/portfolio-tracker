# _scratch — original exploratory scripts (SUPERSEDED)

These are the step-by-step scripts written while building the tool, kept for
transparency/history. **Do not use them** — they have hard-coded paths/tickers.

The single maintained entrypoint is `../../generate.py`, which consolidates all
of this into one parameterized, zero-hard-coding pipeline.

| file | what it did |
|------|-------------|
| parse_hist.py  | first pass: forward-accumulate holdings (revealed the incomplete-history problem) |
| recon.py       | reverse-reconstruct holdings anchored to the Portfolio CSV |
| activity.py    | trading activity + exited positions + monthly cash flow summary |
| fetch_prices.py| batch Yahoo Finance price fetch (hard-coded ticker list) |
| build_site.py  | per-stock average-cost engine + payload JSON |
| make_html.py   | SVG dashboard HTML template |
