# Changelog

This project uses focused Git commits. Each commit should answer one question:
what investor workflow or data contract changed?

## 2026-07-02

- Added a multi-source company financial-status lens for current holdings.
- Integrated the financial-status artifact into the generated dashboard.
- Added FMP/yfinance/SEC-style data-confidence handling with secrets kept out
  of repository files.
- Prepared the repository for public use by keeping personal exports and
  generated runtime artifacts out of Git.

## Current Commit Themes

- Portfolio sync: broker CSV ingestion, verification, and self-contained HTML.
- Market mass: center-of-gravity boundaries, pyramid profiles, and option-spread
  research backtests.
- AI research: AI semiconductor, AI watchlist, and AICS industry-chain scoring.
- Automation: launchd jobs for price refresh, market sentinel, and intraday tape
  judgment.

## Commit Message Convention

Use imperative, scoped messages:

```text
Add multi-source financial status lens
Document market mass option workflow
Ignore local runtime artifacts
Refresh dashboard financial artifacts
```

Avoid committing raw broker CSVs, local logs, screenshots, cache files with API
quota history, or one-off probe HTML files.
