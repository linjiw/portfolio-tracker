# Market Mass / Gravity Iron Condor Status

Generated: 2026-06-26 after the daily bar. Research only; not financial advice.

## Current Build Status

Implemented modules:

- `scripts/market_mass_boundaries.py`: center-of-mass, gravity quality, volatility build-up, and probabilistic boundary zones.
- `scripts/market_mass_credit_spread_backtest.py`: weekly credit-spread and iron-condor model backtester.
- `scripts/options_credit_spread_backtest.py`: user-facing wrapper for the options backtester.
- `README_options_credit_spread_backtest.md`: workflow, parameters, and interpretation notes.

Validation:

- `python3 -m unittest discover -s tests`
- Result: 103 tests OK.

Latest strategy research result:

- Best current family: QQQ options using `^NDX` gravity with QQQ as the volume proxy.
- Strongest model variant: asymmetric gravity condor, not a perfectly balanced income condor.
- Best direct NDX-gravity / QQQ-vehicle run with `overheat` blocked:
  - 32 trades
  - 84.4% win rate
  - +$1,646.79 model P&L on $100,000 starting capital
  - -0.50% max drawdown
- Rolling walk-forward for the same family:
  - 20 trades
  - 85.0% win rate
  - +$1,005.01 model P&L
  - Profit factor 2.66
  - 30.0% short-touch rate
  - -0.30% worst window drawdown

Important interpretation: the model often finds one side of the condor valuable and the other side nearly worthless. That is not a classic balanced income iron condor. It is better described as an asymmetric gravity spread with defined risk on both sides.

## Fresh Market Inputs

Data sources:

- Price and option-chain data: Yahoo Finance via `yfinance`.
- Implied volatility proxy: FRED `VXNCLS`; fallback `VIXCLS`.
- Next-week calendar check: Nasdaq Trader holiday calendar shows U.S. equity and options markets closed on Friday, 2026-07-03 for Independence Day observed.

Practical weekly expiration target:

- Thursday, 2026-07-02, not Friday, because Friday 2026-07-03 is a market holiday.

## NDX Center Of Mass

As of the 2026-06-26 daily bar:

- NDX price: 29,118.24
- Center of mass: 26,561.56
- Quality score: 73.74 / 100
- Regime: `active_center`
- Distance from center: +1.063 z
- Center weight in boundary model: 0.819
- Mass sigma: 9.03%
- Mass quantity ratio: 1.044
- Effective sample raw ratio: 0.622

Quality components:

- Concentration: 28.43
- Local density: 81.46
- Stability: 88.71
- Balance: 85.17
- Effective sample ratio: 100.00

Meaning:

- The market has a real center, because quality is above 70.
- Price is above the center, but not detached. `distance_z = 1.063` is elevated, not escape velocity.
- The low concentration score says price-volume mass is not tightly clustered in one narrow zone; the strong stability/balance scores say the center is still usable.
- Gravity is below current price. That means upper call risk is more relevant than lower put risk right now.

## Volatility And Stored Energy

NDX volatility context:

- Realized vol 10d: 30.50%
- Realized vol 21d: 31.11%
- Realized vol 63d: 23.66%
- VXN: 30.91 on 2026-06-25
- VIX: 18.89 on 2026-06-25
- VXN/VIX ratio: 1.636
- Annual vol used by model: 31.00%

Volatility build-up:

- Build-up score: 24.55 / 100
- Potential energy: 0.565
- Kinetic energy: 0.488
- Momentum z: -0.988

Build-up components:

- Realized vol compression: 0.00
- Implied vol pressure: 45.45
- Distance-from-center pressure: 35.44
- Kinetic pressure: 39.51
- Volume pressure: 1.77

Meaning:

- Build-up is not extreme. The model is not flagging a high stored-energy squeeze.
- VXN is much hotter than VIX, so Nasdaq-specific option risk is elevated.
- Momentum is currently negative while price remains above the center, which fits a pullback / gravity-return setup.

## QQQ Weather Gate

QQQ as of 2026-06-26:

- Close: 706.52
- EMA8: 718.16
- EMA13: 720.06
- EMA21: 718.89
- EMA34: 710.99
- EMA55: 695.31
- ATR14: 16.34
- 5-day return: -4.60%
- EMA21 5-day slope: -0.25%
- Weather classification: `break`

Meaning:

- QQQ closed below EMA8/13/21/34, while EMA21 has started bending down.
- Under the teacher process, this blocks a blind "sell premium because the band looks wide" decision.
- This does not mean panic; it means new short-gamma structures need smaller size, cleaner entry, and explicit invalidation.

## Next-Week Gravity Boundaries

For the holiday-shortened 4-trading-day horizon into 2026-07-02:

NDX 75% boundary:

- Lower boundary: 24,691.62
- Lower zone: 24,580.93 - 24,802.80
- Upper boundary: 29,537.06
- Upper zone: 29,404.65 - 29,670.07

NDX 80% boundary:

- Lower boundary: 24,440.60
- Lower zone: 24,318.57 - 24,563.24
- Upper boundary: 29,840.42
- Upper zone: 29,691.44 - 29,990.16

NDX 95% boundary:

- Lower boundary: 23,182.78
- Upper boundary: 31,459.46

QQQ equivalent direct 4-day boundaries:

- 75% lower / upper: 600.99 / 718.62
- 80% lower / upper: 594.89 / 725.98

Meaning:

- The lower boundary is extremely far below price because the center of mass sits far below the current market.
- The upper boundary is close to spot because price is already above the center.
- This asymmetry says: do not force a normal balanced IC. The model naturally prefers upper call-risk control.

## Next-Week NDX Strategy

Decision label: `WATCH`, not full `ALLOW`.

Reason:

- Gravity quality is good: center exists and build-up is low.
- But QQQ weather is `break`, and July 2 is a shortened-week expiration.
- A blind NDX iron condor at Friday close would be short gamma during a trend-break regime.

Entry gate for Monday 2026-06-29:

- Do not enter in the first minutes of a gap move.
- Prefer entry only if NDX remains below the 29,405 - 29,670 upper boundary zone or rejects from it.
- Block entry if QQQ/NDX flips into `overheat`.
- Block or reduce size if VXN expands sharply above the current 30.91 area or VXN/VIX rises beyond roughly 1.75.
- Block if NDX opens above the 80% upper boundary zone near 29,691 - 29,990 and holds there.

Primary NDX structure if conditions are met:

- Expiration: 2026-07-02.
- Use defined risk only.
- Prefer the call side as the economic side.
- Conservative call spread: sell 29,900C / buy 30,000C.
  - Current chain snapshot: about 19.0 index points credit.
  - Approximate max risk: 81.0 index points.
  - Dollar multiplier: about $1,900 max credit and $8,100 max risk per NDX contract before fees.
  - Approximate short-call delta: 0.198.
- More aggressive call spread only after clear rejection under the 75% upper zone: sell 29,650C / buy 29,750C.
  - Current chain snapshot: about 27.3 index points credit.
  - Approximate max risk: 72.7 index points.
  - Approximate short-call delta: 0.293.
  - This is closer to the 75% upper boundary, so it carries materially higher touch risk.

Put side:

- Model-pure lower-boundary put short would be near or below 24,700.
- Current NDX chain near that area has poor / unattractive spread economics.
- I would not force the lower put wing for income.
- If a platform/order process requires an IC ticket, keep the lower wing very far OTM and treat it as structural risk cap, not as a real profit engine.

Strict two-wing IC alternative:

- A tradeable premium put wing around 28,000P / 27,900P has quoted credit, but it is not outside the final gravity lower boundary.
- That makes it a higher-risk income-condor variant, not the model-pure gravity boundary trade.
- Use only much smaller size if the goal is to study true IC behavior.

QQQ implementation equivalent:

- Expiration: 2026-07-02.
- NDX gravity maps closely to QQQ 75%/80% upper levels around 719 - 726.
- Conservative QQQ call side: sell 725C / buy 735C.
  - Current chain snapshot: about $2.02 credit.
  - Max risk: about $7.98 per share, or $798 per spread before fees.
  - Approximate short-call delta: 0.222.
- Model-pure lower put side near 600P / 590P has almost no net credit.
- A tighter 680P / 670P put wing has about $1.09 credit, but it is not the model lower boundary and increases downside risk.

## Exit And Risk Rules

For any position opened:

- Take profit: close at 50% - 60% of credit captured.
- Stop: close if spread mark reaches about 2.2x entry credit.
- Touch rule: reduce or close if the short strike is touched intraday and price does not quickly reject.
- Hard invalidation: NDX holds above the 80% upper zone, roughly 29,691 - 29,990, or QQQ reclaims into a sharp overheat rally.
- Size: smaller than normal because QQQ weather is `break` and NDX contracts are high notional.

## Algorithm Summary

For each bar:

1. Convert price to log price: `x_t = log(price_t)`.
2. Compute mass:
   - recency decay
   - multiplied by square root of normalized dollar volume
3. Compute center of mass:
   - `COM_log = sum(mass_t * log_price_t) / sum(mass_t)`
   - `COM_price = exp(COM_log)`
4. Score center quality:
   - concentration
   - local density
   - stability
   - balance
   - effective sample ratio
5. Measure distance:
   - `distance_z = (log(current_price) - COM_log) / mass_sigma`
6. Blend mass walls with volatility cone:
   - strong center: boundaries lean toward mass profile
   - weak/detached center: boundaries lean toward realized/implied volatility
7. Trade filter:
   - require usable center
   - avoid high build-up
   - avoid overheat for IC
   - use short strikes outside the selected boundary
   - cap short delta
   - require sufficient credit/risk
8. Backtest with walk-forward validation:
   - do not optimize on all years and declare victory
   - require positive holdout and rolling-window behavior

Current interpretation:

- NDX has gravity, but current price is above the center.
- The lower boundary is too far away to pay much.
- The upper boundary is close enough to matter.
- Therefore, the model prefers upper call-risk positioning, not a forced balanced IC.
