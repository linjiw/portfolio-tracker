#!/usr/bin/env python3
"""Compatibility wrapper for the market-mass credit-spread backtester."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    target = Path(__file__).with_name("market_mass_credit_spread_backtest.py")
    runpy.run_path(str(target), run_name="__main__")
