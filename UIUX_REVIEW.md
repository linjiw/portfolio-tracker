# Portfolio Dashboard — UI/UX + Performance Review & Improvement Plan

> Historical review plan. The current renderer and `tests/test_dashboard_ui.py`
> are the source of truth after the July 9, 2026 accessibility, mobile, data-
> quality, and injection-safety pass.

> **STATUS 2026-06-09 — implemented.** All P0s, the high-value P1s and PERF-2/4 landed:
> `--amber-line` defined (VIS-1) · Google Fonts removed, fully self-contained (PERF-1) ·
> mobile 12px cell floor + 决策一览 stacked-card view replaces the side-scrolling table (MOB-1/2) ·
> two-tier KPI strip (4 decision numbers + demoted ledger) · QQQ/TQQQ tab: 3 decision cards stay
> open, 8 reference cards fold behind `<details>` with a 参考资料 divider (IA-1) · `scope="col"`
> on all `<th>`, axis text → `#888D96`, `color-scheme:dark`, citation opacity fix, search-count
> `#srlive` announcements (A11Y-1/2/3/8, MOB-6) · idle prefetch gated on low-end devices (PERF-4) ·
> per-stock fib ribbons stripped from the payload and recomputed in JS (`fibArrays`), verified
> 0/920 mismatches vs Python and 0/63 `now`-snapshot disagreements — **1,215,559 → 711,297 bytes (-41.5%)** (PERF-2).
> Not done (deliberately): IA-2 tab merge (kept all tabs; rail grouping already shipped), PERF-3
> shared date axis (next-largest win, ~100KB, touches every chart consumer), P2 polish items.

_Reviewed 2026-06-08 against `generate.py` + the rendered `output/portfolio_dashboard.html`._
_Method: 7-dimension review (IA, visual, accessibility, performance, mobile, interaction, content) with adversarial source-verification of every P0/P1 finding, plus live browser instrumentation. 46 findings raised, 45 survived verification, 1 killed as non-reproducible._

## Executive summary

This is a strong, deliberately-engineered single-file dashboard. The architecture is sound (lazy-rendered overview panels, reverse-reconstructed holdings, hand-rolled inline SVG, no view-time libraries) and the accessibility scaffolding is well above average for a hand-built app: exactly one `<h1>`, four landmarks, all 14 tabs wired with `role=tab`/`aria-selected`, all `<svg>` carry `<title>`/`aria-label`, all buttons labelled, and a working skip-link. The problems are **concentrated, not pervasive** — the highest-impact fixes are mostly one line.

For *this* user — a daily trader whose three questions are **"did I make money," "what's my risk," "should I trade today"** — the issues that matter most:

1. **A one-token CSS bug silently breaks the verdict color.** `var(--amber-line)` is referenced at `generate.py:2458` (sync dot), `3020` (今日要点 hero banner border+icon), and `3721` (journal chip) but **never defined** — `:root` only defines `--accent`, `--accent-soft`, `--accent-line`. The amber "1–2 positions need a look" state — the *most common real-world state* — therefore renders near-white and is indistinguishable from "all calm." The whole default tab is built around an answer-at-a-glance banner, and its middle gear is broken.
2. **The decision is buried under reference material.** The QQQ/TQQQ tab is a ~3,000px wall of 13 always-open cards; the "should I trade" answer is in the first two and the rest is options-structure reference. Separately, three of eleven tabs (决策一览 / 持仓信号 / 行为决策) re-slice the same per-holding data, so "which tab is canonical" has no answer.
3. **Mobile is below the readability floor.** The default decision table side-scrolls 181px on a phone with its two key signal columns (行为标记 / 关注度) off-screen, and dense cells render at 9.5–11px.

Two cross-cutting wins: a **render-blocking Google Fonts link** is the only view-time network request (it breaks the self-contained promise and leaks to Google on every open), and **~490 KB of recomputable Fibonacci arrays** (48.9% of the payload) plus ~147 KB of duplicated date strings roughly double the file for data read one stock at a time.

---

## What was tested (live measurements)

| Metric | Value | Read |
|---|---|---|
| File size | 1,215,559 B single HTML | 1.16 MB script (logic + embedded JSON), 34 KB CSS |
| domInteractive / FCP | 53 ms / 504 ms | FCP gated by the Google Fonts fetch |
| DOM nodes | 4,816 | 1,039 `<td>`, 868 `<span>`, **36 tables all built at load** |
| External requests | 1 (Google Fonts, 4 families) | Contradicts the self-contained goal |
| Tabs / sections | 11 main tabs, 42 `<h2>` | Default tab = 3,094px scroll, ~20 sections |
| Mobile type | 11px-dominant, mass ≤10.5px, some 9.5px | Below the readability floor |
| Default table on phone | 480px in 299px container | 181px side-scroll, key columns off-screen |
| `<th>` with `scope` | 0 of 184 | Screen-reader table semantics incomplete |
| Tab-switch | 2.5–25.7 ms | Lazy chart-render works; no jank |

---

## Corrections to the first-pass brief (verified false — do NOT action these)

Adversarial verification overturned three of the initial live-probe claims:

- **The "privacy console.log fires 12×" is NOT a code bug.** `generate.py:3910` is a single, top-level, `if(!_tmOn())`-guarded `console.log`. The 12 lines were the measurement harness re-evaluating the script — not a registration loop. Don't "dedupe" anything; if you want silence, just delete the line.
- **Muted text contrast passes AA.** `--mut #888D96` = 5.87:1, `--faint #7E848E` = 5.20:1 on `--bg`. The real readability problem is **font size on mobile**, not color. The genuine sub-AA color issues are narrower: SVG **axis labels** hard-coded to `#6B7079` (A11Y-1) and **citation fine-print** at `opacity:.65` (A11Y-2).
- **The skip-link is NOT blue-on-dark.** `.skip:focus` at `generate.py:2303` styles `color:var(--accent)` on `--panel2` with a 2px accent border. Already correct.

One finding was killed entirely: **IXD-1** (net-worth curve ends ~1 day below the KPI) does not reproduce in mark-to-market mode — curve and KPI both end 2026-06-05 and match to the cent.

---

## Phased plan

### P0 — now (restore the answer + lean fonts + mobile floor)
- **Define `--amber-line`** (VIS-1) — **S**. Add `--amber-line:var(--accent);` to `:root` (~line 1812). Use solid `--accent`, **not** `--accent-line` (the 0.32-alpha version would render the icon/border dimmer than its green/red peers). Restores the most common verdict color on the hero banner, journal chip, and sync dot.
- **Drop the Google Fonts link** (PERF-1 / IXD-5) — **S**. Delete `generate.py:1777-1779`. Fallback stacks already chain to system + PingFang SC / Noto, so this is zero-risk; makes the self-contained claim true and unblocks first paint. (Inline the 3 Latin faces as base64 `@font-face` if you want the exact look — never inline Noto Sans SC, it's multi-MB.)
- **Raise the mobile type floor + stop the default table side-scrolling** (MOB-1 / MOB-2 / VIS-2) — **M**. Bump mobile `.scroll` cells 11px→12px and floor `th`/`.legacychip`/`.tag`/`.attn-inline` at 11px inside `@media (max-width:560px)` (~line 2328); convert the default 决策一览 score table to stacked labelled cards on mobile so 行为标记 / 关注度 stop scrolling off.

### P1 — next (cut IA overload, shrink payload, close comprehension a11y gaps)
- **Progressive disclosure on the QQQ/TQQQ wall** (IA-1) — **M**. Keep the top two cards open; wrap the other ~11 in native `<details><summary>` (already idiomatic in this file), default the options group closed when there are no option legs.
- **Collapse the three overlapping tabs into roll-up + drill-down** (IA-2) — **M**. Demote 持仓信号 / 行为决策 to a `<details>` drill-down under 决策一览 (porting their unique 共振 / 技术姿态 / nudge-card detail). Cuts 11 tabs → 9. Update `VALID_SEG.ov` / `SEG_LABEL` / seg-rail in lockstep.
- **Always land on the light tab + ration idle prefetch** (IA-4 / IA-3 / PERF-4) — **S**. Seed `initialSeg` from `DEFAULT_SEG.ov` (`generate.py:3238`) so a fresh load never opens straight into the QQQ/TQQQ wall; gate the all-panel idle prefetch on `deviceMemory`/`hardwareConcurrency`.
- **Strip recomputable ribbons + dedupe the date axis** (PERF-2 / PERF-3) — **M**. ~490 KB of per-stock `e5/e8/e13/e21/rsi/mom/state` arrays (`generate.py:758-759`) are read only for the one active stock; recompute in ~30 lines of vanilla JS from `s.prices` on first chart activation (mirror Python's `round(x,2)`). Emit one shared `dateAxis` instead of repeating 182 dates 64×. Roughly halves the file and parse cost — worst-felt on low-end mobile.
- **Axis/citation contrast + table/widget ARIA** (A11Y-1/2/3/4/5/6, MOB-6) — **S–M**. Axis `<text>` `#6B7079`→`#888D96`; drop citation `opacity:.65`; one-pass `scope="col"` setter for all 184 `<th>`; re-role the holdings `listbox`/`tablist`; write filter result counts to the existing `#srlive`.

### P2 — later (hierarchy, interaction states, terminology)
- **One tier of card emphasis + wire the type ramp** (VIS-3 / VIS-2) — **M**. ~20 default-tab sections share identical card weight; give daily-decision cards structural emphasis and make the defined `--t-*` tokens (currently ~0 real uses) the enforced source of truth.
- **Save confirmation, focus-visible rings, draft protection** (IXD-2/3/4/6) — **M**. Journal/review/rebal saves only flip a chip — announce to `#srlive`, restore focus, add `:focus-visible` to clickable rows, warn on unsaved drafts.
- **Re-think the densest mobile tables + landscape width** (MOB-3/4, VIS-5) — **M**. Reuse the MOB-2 card pattern for 持仓信号; key column-drops to actual width; relax `.scroll` max-height in landscape.
- **Canonical naming + consistent glossing + percent precision** (CONTENT-1..7, IA-5, VIS-6) — **M**. One canonical Chinese name per concept (English in the `.nm` subtitle), wrap first-occurrence acronyms in the existing `gl()` glossary, route ~35 ad-hoc `toFixed` sites through the canonical `pct()/ppf()` helpers. Keeps the Chinese-first UI and Fibonacci-as-reference framing.

---

## Quick wins (high impact-to-effort, <1 hr each)

1. `--amber-line:var(--accent);` in `:root` (VIS-1) — the single biggest visual fix.
2. Delete the three font `<link>` lines at `generate.py:1777-1779` (PERF-1).
3. One-pass `scope="col"` setter for all 184 `<th>` in the render hook (A11Y-3).
4. `<meta name="color-scheme" content="dark">` + `color-scheme:dark` in `:root` (A11Y-8) — dark native controls, no white flash.
5. Axis `<text>` fill `#6B7079`→`#888D96` (A11Y-1 / VIS-4).
6. Mobile `.scroll` cells 11px→12px; floor micro-labels at 11px (MOB-1).
7. Seed `initialSeg` from `DEFAULT_SEG.ov` (IA-4).
8. **Don't** chase the "12× console.log" — it's a single guarded call (see Corrections).

---

## Remaining test gaps (worth running next)

- Real **axe-core / Lighthouse** contrast pass against the actual `--panel #121316` chart backdrop to confirm the axis/citation fixes clear 4.5:1.
- **Low-end device profiling** (genuine Android, or Chrome 4–6× CPU throttle + `deviceMemory` override) to measure the ~1 MB object-literal parse cost and validate the payload-shrink moves main-thread blocking time, not just bytes.
- **Screen-reader smoke test** (VoiceOver + NVDA) over the dense tables after the `scope` fix and the holdings sidebar after the role re-work.
- If PERF-2 is implemented, **diff a held stock's recomputed EMA/RSI** against the Python output (must mirror `round(x,2)` or the chart drifts).
- **Re-measure FCP** offline / cold-cache after removing fonts to confirm the CJK fallback renders acceptably.

---

## Validation after each batch of edits

1. `python3 sync.py` (or `--no-fetch`) → `got N/N tickers`, verification block `OK` for marketValue / unrealized / numHeld.
2. `node --check` on the extracted `<script>` (mind nested template-literal backticks).
3. Open the HTML, confirm the payload parses (no console errors), and a held stock shows its price line + buy/sell markers + avg-cost step line.

_Every recommendation respects the hard constraints: single self-contained file, vanilla JS/CSS/SVG, `.replace("__DATA__")` injection preserved, Chinese-first UI and Fibonacci-as-reference framing kept, no telemetry/network added, no hard-coded tickers._
