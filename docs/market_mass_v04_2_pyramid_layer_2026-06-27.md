# Market Mass v0.4.2 Pyramid Layer

Generated: 2026-06-27. Research only; not financial advice.

## Status

Status: `market_mass_v0.4.2_pyramid_layer`

This iteration addresses the concern that a center of mass can look too low
when the market has repriced higher for weeks. The previous engine already used
exponential recency decay, but the dashboard only exposed the selected swing
profile. The new layer compares several time memories before deciding whether
the mass is coherent.

## Time Decay

Mass still uses exponential decay:

```text
recency_weight = 0.5 ** (bar_age / half_life)
```

Default pyramid profiles:

- `tactical`: lookback `63`, half-life `14`.
- `swing`: lookback `84`, half-life `21`.
- `structural`: lookback `252`, half-life `63`.

That means the swing profile gives bars 21 trading days ago 50% weight, 42 days
ago 25% weight, and 84 days ago 6.25% weight. The tactical profile reacts
faster; the structural profile preserves older accepted participation.

## New Outputs

Each symbol in `output/market_mass_dashboard.json` now includes:

- `pyramid.profiles`: tactical/swing/structural profile summaries.
- `pyramid.agreement.centerSpreadPct`: percent spread among profile centers.
- `pyramid.agreement.centerDisagreementZ`: profile-center disagreement in
  average mass-sigma units.
- `pyramid.agreement.agreementScore`: 0-100 agreement score.
- `pyramid.massHealth.score`: 0-100 composite health score.
- `pyramid.massHealth.label`: `coherent_mass`, `working_mass`,
  `fragile_or_transition`, or `low_friction_or_no_mass`.
- `pyramid.massHealth.frictionLabel`: `strong_friction`, `friction_present`,
  `weak_friction`, `low_friction`, or `low_friction_escape_risk`.

## Interpretation

A center is built well when the single-profile quality is strong and the
pyramid agrees:

- Good/stable: `quality_score >= 70`, stability component near or above `70`,
  `abs(distance_z) <= 1.5`, `gravity_score >= 60`, `levitation_score < 45`,
  `centerDisagreementZ < 0.5`, and center spread below roughly `7%`.
- Usable: `quality_score >= 55`, `abs(distance_z) <= 2.5`, and pyramid
  agreement remains moderate.
- Weak/low-friction: `quality_score < 40`, `abs(distance_z) > 2.5`,
  `levitation_score >= 65`, `center_weight < 0.25`, elevated build-up, or
  `centerDisagreementZ >= 1.25`.

The key distinction is:

- `quality_score` answers whether one center is mathematically well formed.
- `massHealth` answers whether several time memories agree enough to trust it.
- `frictionLabel` answers whether price likely still feels mean-reversion
  resistance from accepted participation.

## Dashboard Changes

- Anchor lanes now display mass health and friction.
- Selected symbol cards include `Mass health`, `Friction`, and `Pyramid
  agreement`.
- Selected symbol detail includes a pyramid table with center, quality,
  stability, build-up, distance, gravity, levitation, reliability, and regime
  per profile.
- The comparison table includes mass health, friction, and agreement columns.

## Validation

Focused validation target:

```text
python3 -m py_compile scripts/market_mass_dashboard.py generate.py
python3 -m unittest discover -s tests -p 'test_market_mass_dashboard.py'
python3 scripts/market_mass_dashboard.py --anchor-only --out-json output/market_mass_dashboard.json
python3 generate.py --no-fetch
```

Full-suite status remains separate because the repository previously had an
unrelated AICS delta test failure.
