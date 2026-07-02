# Methodology

This project is built around a strict separation between public framework code
and private account data.

## Data Contract

The generator expects two local broker-export concepts:

- position snapshot: current holdings, quantities, latest broker mark, current
  value, unrealized P&L, and cost basis
- account history: dated buys, sells, deposits, dividends, interest, and option
  transactions

The parser is header-aware for activity files and defensive for position files:
it accepts alphanumeric account identifiers, sums the same ticker across
accounts, skips money-market core rows from equity totals, and treats option rows
as separate account-exposure data.

## P&L Rules

- Unrealized P&L comes from the latest position snapshot for broker-exact syncs.
- Realized P&L is computed inside the activity window using average cost.
- Positions that existed before the visible activity window are marked as legacy
  lots; their starting cost is estimated from the first available market price.
- `--mark-to-market` keeps shares and cost basis from the broker snapshot while
  refreshing held-equity marks from Yahoo prices.

## Verification Gate

`sync.py` regenerates the dashboard, then independently re-sums held-equity
market value and unrealized P&L from the position snapshot. If the dashboard
does not match the broker export within tolerance, the sync fails.

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

## Market-Mass Research

`scripts/market_mass_boundaries.py` estimates a recency- and dollar-volume-
weighted center of accepted participation in log-price space, then blends mass
walls with realized/implied volatility to create probabilistic boundary zones.

`scripts/market_mass_credit_spread_backtest.py` uses those zones to research
defined-risk credit spreads and iron condors. It is a model-priced research
tool, not a historical option-fill tape or live trade signal.

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
