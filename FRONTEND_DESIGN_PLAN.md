> **ROUND-3 STATUS:** scores after round 2 — typo 7.5 · color **8.0** · motion 7.0 · spatial 7.5 ·
> atmosphere 7.0 · distinct **8.0** · dataviz 7.5 (avg 7.5; was 5.5 → 7.2 → 7.5). ACTIVE plan below = round 3.
> **ROUND-3 STATUS (2026-06-10): IMPLEMENTED.** A1-A13 (true minus, bg2 depth, 2×2 mobile KPIs, chip demotions,
> prose measure, shared time rail, chart-truth one-liners, house verdict mark, onboard slot) · B1-B6 (Barometer:
> no-amber state ramp + 8px + tappable + named in masthead + single home; lead-card bilingual shoulders; record-tab
> two-tier pass; draw-once revisit gate + tape sweep + KPI settle; chipBd convention; tap-to-pin) · C1-C5 + C4
> (grain at spec + tested; registration corners + ranked frames; tooltip glass; linked crosshair; allocation rulers).
> Deviations: C6 rail spine skipped again (interactive nav-layout toggle = outsized regression risk for a P2);
> B5's FIBCOL/QTC-from-C deferred (adjacent consts, already lint-covered). 807,511 B (+69KB of +120KB cap).

# Graphite Atelier — Design Improvement Plan (Round 3)

_Synthesized 2026-06-10 from the verified 7-dimension design review of `generate.py` (template ≥1782) / `output/portfolio_dashboard.html` (711,297 B baseline). Sharpened against `output/audit_shots/_DESIGN_REF.md`; does not duplicate the shipped `UIUX_REVIEW.md` (June-8) items._

---

## 1. Scorecard

| Skill axis | Score /10 | Verdict |
|---|---|---|
| Typography | 7.5 | Fonts embedded, numerals meticulous, three voices real — but ramp/tracking drift and an ASCII hyphen-minus betray the "engraved numeral" claim, and the bilingual lockup is a one-line cameo. |
| Cohesive color | 8.0 | The strongest axis: token-derived palette, lint-enforced, semantic discipline holds — but pure amber leaks into regime-state encoding and two status chips, and the depth rung of the surface ladder doesn't render. |
| Motion | 7.0 | A named, reduced-motion-disciplined choreography exists; the defect is economy — draws replay on every re-glance, the signature band has no entrance, KPI numerals never settle. |
| Spatial composition | 7.5 | Score/qt/nw tabs have real two-tier hierarchy and the ruled ledger; journal/rebal/sig regressed to flat equal-card stacks, prose runs ~1,070px unbounded, mobile first screen is chrome. |
| Atmosphere | 7.0 | The concepts (grain, bloom, recessed plates) all shipped — at sub-perceptual strength. Two rounds running, because nothing measures it. |
| Distinctiveness | 8.0 | THE signature (masthead Regime Barometer) exists on every tab — but it is mute, inert, unnamed, and echoed 70px below by a near-identical twin. |
| Data visualization | 7.5 | Craftsman-grade marker/axis/legend kit; the residue is truth-and-alignment details: misaligned time rails, no touch readout, ungrouped axis dollars, yearless tooltips. |

**Honest read.** This product clears the "refined minimalism executed with precision" bar in concept everywhere and in execution about 80% of the way: the identity is real, ownable, and already documented in its own CSS comments. The single biggest gap is that **the last 20% of every signature is unshipped or sub-perceptual** — the grain is 3× below its own acceptance target, the signature band can't be touched or named, the embedded display face appears in one place, amber still has three squatters, and the record tabs missed the hierarchy pass. Nothing here needs a new direction; everything needs the existing direction turned up to its own stated spec and then locked by measurement.

---

## 2. What already works — do not regress

- **Self-contained type system**: Space Grotesk 600 + IBM Plex Mono 400/500/600 as base64 WOFF2 Latin subsets, unicode-range gated so CJK falls to PingFang (~24.5KB, generate.py:1806-1809); weights shipped exactly match weights used; zero view-time network.
- **Meticulous numerals**: `tnum`+`zero` at body level (1873), `font-variant-numeric` belt-and-suspenders on every numeral surface, SVG text forced to mono+tabular (2182). Verified rendering at 4× zoom.
- **Three-voice typography**: Space Grotesk caps masthead shoulder (1908), Plex Mono as the data voice (convention codified at 2095), uppercase micro-caption system on `--ls-label`. The bilingual CJK + Latin-caps lockup is the ownable signature.
- **Runtime-derived chart palette**: `C` built from `getComputedStyle` at boot (2842-2846) so SVG can't fork from CSS tokens; `scripts/_audit_shots.py:97-117` lints it.
- **Semantic color discipline that actually holds**: green/red strictly P&L-sign/risk-direction across all audited tabs; two-tier amber (live `#E8B339` vs reference `#B89030`) is a working system; amber pixel coverage measured 10-20bp per tab.
- **THE signature**: masthead-docked QQQ Regime Barometer — full-bleed run-merged weather band + glowing 2px amber today-tick, on all 11+4 tabs at every scroll position (1909-1915, 4302-4311); the run-merged strip motif recurs as house grammar on 5+ surfaces via one `qStateColor`.
- **KPI ruled ledger** (1937-1996): one engraved strip, not floating stat cards; hero cell with animated amber underline tick; demoted tier-2 band on bg2.
- **Two-tier card grammar where applied**: `.card.t1` top rule, recessed `.card.ref`, foldcards + dashed `ref-divider`, ≥1200px paired-fold grid on qt/struct (2272-2316).
- **"The Morning Open"** load choreography: single `--ease`, fully gated under `prefers-reduced-motion`, measured-length pen-draws via `armDraw`, stamped verdict rail, post-load navigation correctly de-staggered via `body.done`.
- **Atmosphere vocabulary in place**: 4-step surface ladder, printed-vs-floating shadow logic, tick family, dashed dividers, amber `::selection`, styled scrollbars, mask-fade list bottoms, 0.07 amber wash under the net-worth curve.
- **Chart kit**: 1/2/5 `niceTicks`, sqrt-area campaign-clustered trade markers (shape = side, not hue), halo'd collision-relaxed end labels, truthful line-style legend swatches, run-merged state strips, plain-language empty states.
- **Deliberate mobile**: content-before-rail stacking, chrome diet, sticky seg-rail, stacked `.smc` decision cards — nothing side-scrolls.
- **House vocabulary**: 真金白银桥, 日线天气图, 老师 Decision Brief, mono provenance colophon. No template produces these.

---

## 3. The aesthetic thesis

**Graphite Atelier, sharpened: a printed instrument, not a dashboard.** One graphite sheet with real tooth, data inked onto recessed plates with registration marks, ledgers ruled by hairlines that rank below their frames, Swiss bilingual setting (CJK leads, Latin caps shoulder it), and exactly one saturated color — amber — meaning exactly one thing: **now / you / here**. Calm is achieved by economy, not absence: every glyph (true minus, slashed zero), every rule weight, and every motion beat is either load-bearing or deleted. The first look each morning is theater; every glance after is instant.

**THE signature move — "Amber means now, and the Barometer answers."** A synthesis of the color and distinctiveness bold moves, because they target the same surface: make the masthead Regime Barometer the product's one unmistakable instrument. Demote overheat-state amber to the chart ramp so the band becomes a muted field with **one** glowing pure-amber now-tick; raise it 6→8px; make it tappable/focusable (→ QQQ 决策台); give it a permanent mono name (regime word + days-in-state); delete its duplicate echo 70px below. Net effect: on every tab, exactly three pure-amber things exist — the masthead tick, the band's now-marker, and your own live line in charts. That is "dominant color with one sharp accent" executed literally, for <500 net bytes. Chosen because it closes two P1s and a P2 across the two highest-scoring axes, converts passive trim into the single interaction a user retells ("the terminal with the weather band"), and is pure sharpening of what already ships.

The other bold moves: typography's bilingual-lockup extension and motion's revisit-gate go to **Phase B**; atmosphere's measured-grain lock and dataviz's linked crosshair go to **Phase C**; spatial's "Annotated Ledger" marginalia grid is **cut** (page-wide layout rework, high regression risk; A7+A8+B3 deliver most of its gain — see non-goals).

---

## 4. Phased plan

Total added-byte budget across all phases: **≈ +5.5KB** against the ≤120KB allowance (fonts already shipped inside the 711KB baseline). Every item: vanilla CSS/JS/SVG, single file, `node --check` must stay green, `.replace("__DATA__")` untouched, no hard-coded tickers, no new hues.

### Phase A — Quick wins (each S, <1h, immediate visual payoff)

**A1. True minus sign in all numeric formatters** — closes TYP-2 (P2, typography). In `generate.py:2592` change `(n<0?'-$':'$')` to `(n<0?'−$':'$')`; in `pct` (2594) and `ppf` (2595) emit `'−'` for `n<0` instead of relying on `toFixed`'s hyphen (e.g. `(n>=0?'+':'−')+Math.abs(n).toFixed(2)`); same for `fmtN` (2593). U+2212 is already subset into all three mono weights (1807-1809); static text (3075, 3088, 3148, 4053) and the formatter at 3717 already use it. Effect: every red KPI gets the full-width engraved minus; page-wide self-consistency. 0 bytes.

**A2. Make the recessed-plate depth rung actually render** — closes COL-2 (P1, color). At `generate.py:1834` change `--bg2:#0E1013` → `#0A0C10`. Measured plate-vs-card delta is ~3 RGB today — invisible under the grain. Verify the other `--bg2` consumers (1976 KPI tier-2 band, 2041, 2272 `.card.ref`, 2609, inputs 3688/3821/3857/3863/3872) still read recessed-not-black; re-run the `_audit_shots` palette lint (a `:root` edit passes cleanly). 0 bytes.

**A3. Fix the 真金白银桥 label collapse** — closes DV-2 (P1, dataviz). In `bridgeCard`, both emit sites `generate.py:3219` (leg) and `3222` (合计盈亏) get `style="width:auto;min-width:150px"` on the `fsym` span, exactly matching the precedent at 3175. Kills the 5-line vertical CJK stack on desktop and mobile. ~60 bytes.

**A4. Restore the 2×2 mobile KPI grid** — closes SP-6 (P2, spatial). Delete the `grid-template-columns:1fr` override at `generate.py:2477` (the ≤980px block at 2459-2464 already provides 2-col) **and** revert the 1-col border overrides at 2483-2485 (`border-left:0!important` + the stray `nth-child(-n+2)` top rules) so the 2-col divider logic at 2461-2464 applies. Values fit: 18px clamp + `overflow-wrap:anywhere` (2486) already guard. Pulls the first decision card ~180px up to the fold. Negative bytes.

**A5. Demote the two status-chip color leaks** — closes COL-5 (P2, color). `.legacychip` (2319-2323): `color:var(--accent-ref); background:none; border:1px solid var(--chip-bd-amber)` — a data-quality caveat must not wear the live/active tier. The 已记录 chip at `generate.py:4018`: `color:var(--mut); border-color:var(--line)` — "recorded" is a neutral done-state, not P&L. Keep the amber 未记录 nudge as the attention case. 0 bytes; reduces amber, frees green for P&L-only.

**A6. Demote the mobile stale-chip** — closes DX-5 (P2, distinctiveness). In the ≤980px block (from 2461): `#stalechip{color:var(--mut);border-color:var(--line)}` plus a 6px amber-dot `::before` prefix; keep `role=alert` + title attr (chip injected at 2612). The largest above-the-fold amber element on a phone becomes ops-quiet. ~120 bytes.

**A7. Cap the prose measure** — closes SP-1 (P1, spatial). Near `generate.py:2268` add `.card .note,.foldbody .note{max-width:640px}`. Use **px, not ch** (68ch ≈ 425-470px in this stack — wrong math). Inline `<span class="note">` uses (3385, 3432) are unaffected; verify `.trail .note` (2018, ellipsis one-liner) isn't visibly truncated or scope it out. Ragged-right whitespace becomes a composition feature. ~50 bytes.

**A8. Pair up nw's folds** — closes SP-4 (P2, spatial). Add `.seg[data-seg="nw"]:not([hidden])` to all three fold-grid rules at `generate.py:2277-2281`. Same component, same rhythm as qt/struct. ~60 bytes.

**A9. One shared time rail for stacked charts** — closes DV-1 (P1, dataviz); prerequisite for C4. Declare `const AXM={mL:CHART_MOB?50:66,mR:CHART_MOB?60:82}` **after line 2847** (`CHART_MOB`'s declaration — placing it after 2846 is a TDZ ReferenceError that `node --check` will NOT catch). Consume in `svgLines` (2936), `fibChart` (3917), `qqqStrategyChart` (2983). `chart()`'s mR:120 stays (needs 现价 room, never stacked). Verify with a plate-edge pixel probe on pfib/stk-fib. ~80 bytes.

**A10. Chart truth pass — six one-liners** (dataviz/color P2s):
- **DV-8**: `fmtTick` (2858) → group ≥$1,000: `return v>=1000?v.toLocaleString('en-US',{maximumFractionDigits:0}):v.toFixed(step>=1?0:(step>=0.1?1:2));` and route `fibChart`'s default y-formatter (3916, `'$'+v.toFixed(0)`) through `fmtTick` too.
- **DV-6**: shared `fd=(dt,multiYear)=>…` next to `monthTicks`, `multiYear = xmin/xmax years differ`; use in the five tooltip row builders (2908, 2971, 3008, 3042, 3956) → cross-year hovers read `25/11/14`.
- **DV-7**: legend squares → real glyphs at 3563 and 3981: `<span style="color:#4FB286">▲</span>金叉 / <span style="color:#E5707A">▼</span>死叉` (U+25B2/25BC already in the subset).
- **DV-5**: `svgLines` marks (2947) get the fib chart's age fade: `const o=0.15+0.35*Math.max(0,Math.min(1,(+new Date(g.date)-xmin)/((xmax-xmin)||1)))` replacing constant 0.4 (`xmin/xmax` in scope at 2937).
- **DV-4**: nwChart strip label (3040) → `${MOB?'动能':'组合动能'}` so mobile stops clipping to "…合动能".
- **COL-3**: EMA zone labels at 2993 → `'拿回区','期权区','防线'` (canonical vocabulary from 3335-3337; do **not** touch the `'EMA8 拿回'` trigger string matched at 3280/payload 1015). Color stays — fills are legitimately action-semantic; only the text disagreed.
~150 bytes total.

**A11. Dead motion wiring** — closes MO-6 (P2, motion). Remove `draw` from the root class list at `generate.py:3044` (descendant-only selector at 2429 never matches it); add `draw:1` to the volatility def at 3639 — the only amber lead series in the app that just appears. It inherits B4's revisit gating automatically. 0 bytes.

**A12. Verdict opens with the house mark, not a dingbat** — closes DX-6 (P2, distinctiveness). Replace the `heroIco` ✓/⚠ span (assigned 3259-3262, rendered 3274) with a 3×17px rounded vertical bar filled with `heroTone` — same construction as `h1::before` (1917-1920). U+2713/U+26A0 are not in the embedded subsets, so today the signature tier opens in a system-fallback face (possibly color emoji). Severity stays triple-carried (rail + tick + count). Leave the fetchwarn ⚠ (2609) and the l4 ✓ (3279) alone. ~40 bytes.

**A13. Move the onboarding strip out of the hero zone** — closes SP-5 (P2, spatial). Add `<div id="onboard-slot"></div>` after the `.kpis` strip (after `generate.py:2562`); retarget `onboardStrip()` in all **three** render paths: 3490 (insufficient-data), 3491 (main), and the 不再显示 dismiss handler at 4046. Do **not** demote the 从这里开始 amber (shipped A13 decision, plan-documented). Verdict → KPI ledger become adjacent again. ~80 bytes.

### Phase B — The identity pass

**B1. THE SIGNATURE: "Amber means now, and the Barometer answers"** — closes COL-1 (P1), DX-1 (P1), DX-2 (P2). Five coordinated edits:
- (a) `qStateColor` (2977): `overheat:'#E8B339'` → `'#C99A3A'` (the chart-ramp `C.e8w` — **not** `C.ref`, which would merge overheat into the cost-basis/reference tier). This recolors the masthead rects (4309-4310), weather-strip (3002), tooltip swatch (3008), and hero ribbon (deleted in (e)). **Rewrite the deliberate-decision comments at 2975-2976** — this overturns a documented choice, on the argument that the latent tick-dissolve hazard (pure-amber tick on a pure-amber overheat run, brightness-only separation on a 2px element) outweighs the state's claim to the live accent.
- (b) Raise the tape 6→8px at 1912 so the signature registers at a glance.
- (c) Make `#qbar` an instrument: `role="button" tabindex="0"` at the markup (2558), click/Enter → `ovGo('qt')` (2771), plus an explicit `#qbar:focus-visible` rule cloning the amber outline pattern at 2200. Keep the title-attr legend.
- (d) Name it: append a mono caption — regime word + `第N天`, colored by `qStateColor`/`qStateLabel` (2977/2980) — **inside the h1 after `.h1-en`** (or a `flex:none` masthead span), NOT in `#rangelbl`: `header .sub` is `display:none` at ≤560px, exactly where hover is unavailable.
- (e) Delete the duplicate hero-row ribbon `<svg>` in `insightBanner` (3264-3270); keep a text caption `QQQ 60D · <state word>` in mono `--faint` with the state word colored by `qStateColor`. One band, one home. (This too revisits a shipped "labeled echo" decision — superseded by (d) giving the band a permanent name.)
Effect: exactly three pure-amber elements product-wide; the signature becomes tappable, nameable, singular. Net ≈ +400 bytes (ribbon deletion offsets).

**B2. Extend the bilingual lockup down the hierarchy** — closes TYP-5 (P2); typography bold move. Two parts:
- (a) Give the LEAD card of each overview tab (and the detail header zone) an English Space Grotesk shoulder line beside its CJK title using the exact shipped `.h1-en` treatment (10px/600/.14em caps/`--faint`, 1908): 决策一览 DECISION BOARD · 全账户净值 NET WORTH · 风险 RISK LEDGER · QQQ/TQQQ DECISION DESK, etc. **One per tab, never every card** — rationed. Zero font bytes (caps subset already covers it); ~200 bytes markup.
- (b) Fix the detail-header voice break at `generate.py:4015`: emit `<h2 class="t t-sym">` + `<span class="nm nm-co">`, add `.dh .t-sym{font-family:var(--f-mono);letter-spacing:.01em}` (ticker = data voice, per the 2095 convention) and `.dh .nm-co{font-size:var(--t-2xs);letter-spacing:var(--ls-label);color:var(--faint)}` (the raw broker string becomes the designed caps line). **Never restyle bare `.nm`** — it carries CJK subtitles on virtually every card (3061, 3074, 3147, 3298…). ~150 bytes.

**B3. Two-tier grammar on the record tabs** — closes SP-2 (P1), SP-3 (P1), SP-7 (P2). Extend the shipped score/qt/nw grammar:
- **journal** (`journalCard` concat at 3842): tag 每周复盘 `.t1`; insert `ref-divider`; demote killerStatCard, emotionOutcomeCard, journalHonesty to `details.foldcard` via the `foldCard` helper (3298) — journalHonesty already wraps an inner `<details>` (3840): convert it, don't double-fold. Default-closed (rerenderJournalTab at 3851 rebuilds innerHTML on save, resetting fold state — acceptable).
- **每周复盘 form**: wrap the five `ta()` emissions (3821, concat 3826) in `<div class="rv-form">` and the facts badges (3824) in `<div class="rv-facts">`; at ≥1200px `grid-template-columns:minmax(0,640px) 1fr` with `.rv-facts` sticky in column 2. Markup wrap is **required** — labels/textareas are interleaved siblings, CSS-only grid would scatter them. `#rvSave/#rvClear` lookups (3846-3850) are querySelector-based, unaffected.
- **rebal** (`rebalOutput`, rows at 3713-3716): keep the verdict banner (3709-3710) and breach-only 动作清单 (3722-3724) as-is; in the deviation card, render breaching rows (`rows.filter(r=>!r.inB)`) first and collapse the in-band remainder behind a default-closed foldCard `其余 N 只在容差内 ▸` (rerenderRebal 3736 resets state — default-closed mandatory). **No per-row `.t1`** — it's a card-level class; amber-vs-grey row coloring already marks actionability. Tab height drops ~700px.
- **sig**: tag positionSignalsCard `.t1`, divider, demote trailing analysis folds — mirroring the qt pattern at 3408.
~1.5KB total CSS+markup.

**B4. Motion economy: first look = theater, every re-glance = instant** — closes MO-2 (P1), MO-3, MO-4, MO-5 (P2s):
- (a) **Revisit gate** in `activateSeg`'s forEach (2740): `if(!p.hidden){if(p.dataset.seen)p.classList.add('revisit');p.dataset.seen='1';if(was)p.classList.add('segin');}` — the unconditional `seen` mark covers the default panel (rendered visible at 3497/4013, where `was` is never true). In the no-pref block: `.seg.revisit svg .draw,.seg.revisit svg .draw2{animation:none;stroke-dasharray:none;stroke-dashoffset:0}` and `.seg.revisit .fbar .p{animation:none}`. Specificity checked: (0,3,1) beats (0,2,2). **Declared: this overturns the documented replay-as-intent decision (plan:201/:265)** — justification: today's answer at the curve's right edge is withheld ~1.3s on every one of a daily trader's dozens of re-glances; first-reveal-per-render keeps the full theater.
- (b) **Tape sweep**: `body.ready #qbar svg{transform-origin:left;animation:tickIn .6s var(--ease) .5s backwards}` (svg is `flex:1` at 1914, so scaleX doesn't move the now-tick); add `#qbar svg{animation:none}` to the reduce block (2435-2444). The ribbon-flick half of MO-3 is moot — B1(e) deletes the ribbon.
- (c) **KPI numerals settle**: `body.ready .kpi .v{animation:dataFlick .32s ease-out backwards}` with nth-child delays .24/.28/.32/.36s (keyframes already at 2390; markup `.kpi .v` at 2635). Pure opacity. Reduce-block entry: `.kpi .v{animation:none}`.
- (d) **Gauge-row hover answers**: extend 2343 to `transition:width .3s var(--ease),filter .15s var(--ease)`; add `.frow:hover .fbar .p{filter:brightness(1.18)}` + `.frow:hover .fsym{color:var(--txt)}`. Modulates each bar's existing semantic color — no amber spent, no reduce entry needed.
~600 bytes.

**B5. Token hygiene — make the system's claims true** — closes TYP-3, TYP-4, COL-4 (P2s):
- **Type ramp** (comment at 1824-1827 claims 5 sizes): replace literal 11px (1922, 2032, 2150, 2349, 2527, 2536, inline 3517/3519) with `var(--t-xs)` and 10px (1908, 2099, 2110, 2317, 2320, 2338) with `var(--t-2xs)`; fold the 14px sites (2291, 2294, 2521, inline 3776) to `var(--t-base)` or mint a documented `--t-base2:14px`; the 18px sites (2222, 2486, 2516) → a tokenized **mobile-numeral pair**, not a blind fold to `--t-lg` (2486-2487 are mobile overrides of `--t-num/--t-num-hero`; 17px@3274 and 20px@2487 map to `--t-lg/--t-xl`).
- **Letter-spacing** (rule comment at 1828): add `--ls-wide:.14em` consumed by `.h1-en`; fold `.kpi .l` .085em (1959), `.ref-divider` .05em (2313), and mobile `header .sub` .04em (2472) into `var(--ls-label)` where optically negligible; **do not touch** desktop `header .sub`'s .11em (1923); rewrite the 1828 comment to name the real exceptions (`.tag` .04em, buttons .02em, `--ls-wide`).
- **Palette single-sourcing**: rebuild `FIBCOL` (3536), `momColor` (3538), `QTC` (2979) from `C` (defined before all three); replace inline `#4FB28666/#E5707A66/#E8B33966` chip borders (3128, 3130, 3141, 3193, 3340, 3385, 4018) with `var(--chip-bd-*)` (valid in style attributes; tokens at 1844); replace raw `#E5707A/#4FB286/#E8B339` at 3122-3126, 3158, 3164, 3042 with `C.*`/tokens. **Extend `lint_palette` (scripts/_audit_shots.py:97-117) to also match 8-digit alpha hex** and to grep the template for raw semantic hex outside `:root` and the `C` ramp — value-level linting currently can't see reference-level drift.
~0 net bytes. Effort M total.

**B6. Tap-to-pin chart readout on touch** — closes DV-3 (P1, dataviz). In `bindCharts`, add a passive `touchstart` on `svg.xh` that calls the existing `place(svg,{clientX,clientY})` once per tap and pins crosshair+tooltip until a tap outside (reuse `clear()`); no `touchmove`, so scroll/pinch untouched. **Mandatory companion edit**: the document-level touchstart hider at `generate.py:4129` fires in the bubble phase after the svg handler and would instantly re-hide the tooltip — extend its guard to keep the tooltip when `ev.target.closest('svg.xh')`. ~0.3KB. Today no svgLines/nwChart pane has any designed touch readout (only `chart()`'s `.mk` markers, 2932/4128).

### Phase C — Polish & atmosphere

**C1. Print the grain at its own documented strength, then lock it** — closes BG-1 (P1); atmosphere bold move. Raise `body::after` opacity .06→.10 (1799) and the `--grain` feFuncA gamma exponent 1.6→2.2 (1856) — gamma shaping keeps it speckle, not a brightness lift. Acceptance: flattest 40px panel window std 3-4 RGB in a 1× capture (the shipped target the last two rounds missed by ~3×; current measured ≈1.1). **Add that exact assertion (crop flattest window, assert 2.5≤std≤6) to `scripts/_audit_shots.py`** beside the palette lint, so atmosphere becomes a tested property of the build. Caution: grain rides z-index 30 above text — the no-text-contrast-regression check is mandatory. 0 payload bytes; ~400 bytes of audit script.

**C2. Engrave the plates: registration marks + ranked frames** — closes BG-2 (P1), BG-5 (P2). First add the missing palette key `line:v('--line','#23262C')` to the `C` IIFE (2844) — **`C.line` does not exist today; without this both edits emit `stroke="undefined"`**. Then: (a) a shared helper next to `niceTicks` (2849) returning four 6px L-shaped corner strokes in `C.line`, appended after the frame rect in all **five** constructors (2893, 2951, 2998, 3028, **3930** — fibChart was missed by the original finding); (b) promote the five frame-rect strokes from `C.hair` to `C.line`, leaving interior grids on `C.hair` — the page's structural/sub-rule hierarchy, reproduced inside the sheet. Declared: (b) revises the plan-documented "hairline plot-frame" choice. ~330 bytes.

**C3. Give the tooltip the house glass** — closes BG-4 (P2). At 2187: `color-mix(in srgb,var(--panel2) 88%,transparent)` + `backdrop-filter:blur(10px)` (+ `-webkit-` prefix); keep `--sh-tt` and the 1px border. The most-summoned floating element joins the masthead/ctx-pill material family. Smoke-test crosshair-drag smoothness on the QQQ chart (blur re-rasterizes per mousemove). ~80 bytes.

**C4. Linked-pane crosshair across stacked technical charts** — dataviz bold move; requires A9. In `bindCharts`' `place()` (4134), after positioning the hovered chart's crosshair, loop the other visible `svg.xh` in the same card whose `data-x0/data-x1` match, reuse the 3-line binary search (4143) for the same epoch, set their `.cx/.cxd` — suppress their tooltips (one floating readout, synced hairlines). Hovering the price ribbon shows where momentum and RSI stood that day — the cross-confirmation read the 多指标共振 panel teaches in prose, made physical. Works on pfib, stk-fib, and oscillator/RSI pairs for free. ~0.5KB, amber hairlines only, calm by construction.

**C5. Engrave the allocation bars** — closes DX-3 (P2). **Scoped to `.acrow .fbar` only** (selector exists at 2335): 10% hairline graduations via `repeating-linear-gradient(90deg, transparent 0 calc(10% - 1px), var(--hair) calc(10% - 1px) 10%)` + a 2px brighter terminus tick on `.acrow .fbar .p::after`. Never the shared `.fbar` — diverging signed bars (3069, 3124, 3127-3128) carry a `.z` zero-line and grow leftward; a right-pinned tick would lie. Fills stay grey: instrument ruling, not color. ~120 bytes.

**C6. Score-tab rail spine (ship the skipped C10)** — closes DX-4 (P2). On the score seg only: collapsible 56px ticker spine (state dot + mono symbol), microbtn toggle persisted to localStorage using the live `rememberSeg/lastSeg` pattern (2697-2698; also `rememberList` 4218 — the previously cited :2666 is now `encodeRoute`). Returns ~244px to the decision table and breaks the two-equal-panes admin pattern with deliberate asymmetry. Effort M; ~700 bytes.

---

## 5. Explicit non-goals

1. **Re-sequencing the Morning Open stagger to .38-.66s** (motion bold-move part 1 / killed MO-1) — the nth-of-type override IS the shipped design (plan A7); at most reword the 2397-2398 narrative comment.
2. **Embedding Archivo** (killed TYP-1) — twice-adjudicated non-goal; body copy is CJK-dominant, the Latin stack is a documented zero-byte progressive layer, and the real cost is ~16KB, not 5.6KB.
3. **Touching the bottom-left counter-bloom** (killed BG-3) — the "flat lower half" was a full-document-capture artifact of `position:fixed`; in a real 1440×900 viewport it already exceeds its own +4L acceptance bar.
4. **Embedding any CJK face** — multi-MB; PingFang/Noto fallback is the design.
5. **The full "Annotated Ledger" marginalia grid** (spatial bold move) — a page-wide two-column layout rework across 11 tabs for a composition gain that A7 (measure cap), A8 (fold pairing) and B3 (record-tab hierarchy) deliver ~80% of at a fraction of the regression risk. Revisit only if the record tabs still read flat after B3.
6. **Demoting 从这里开始 from amber to `--mut`** — contradicts the shipped A13 decision; relocation (item A13 here) solves the spatial problem instead.
7. **Restyling bare `.nm` or shared `.fbar`** — both are shared classes (CJK subtitles everywhere; diverging signed bars); all styling goes through scoped modifier classes (`.nm-co`, `.acrow .fbar`).
8. **`ch`-unit measure caps** — `ch` ≈ 0.5em in this stack; CJK measure math must be in px.
9. **Putting the Barometer caption in `#rangelbl`** — `header .sub` is `display:none` ≤560px; the caption lives in the h1 lockup.
10. **Any decorative green/red, any new hue, any gradient-as-decoration, any always-running animation** — the identity is rationing; green/red stay P&L-sign/risk-direction only, amber stays "now" only, and the Fibonacci panels keep their technical-analysis-reference (非投资建议) framing throughout.

---

## 6. Validation protocol (after every phase)

1. `python3 sync.py --no-fetch` — must print `got N/N tickers` and the verification block `OK` for marketValue / unrealized / numHeld. A MISMATCH means the dashboard is wrong; stop.
2. `node --check` on the `<script>` extracted from `output/portfolio_dashboard.html` — the template nests many template literals; this is the only JS correctness gate. (Remember it will NOT catch runtime TDZ errors — A9's const placement must be code-reviewed, then smoke-tested in the browser console.)
3. Re-run `python3 scripts/_audit_shots.py` — palette lint must pass (extended per B5 to 8-digit alpha + raw-hex greps; extended per C1 with the grain-std assertion). Eyeball `ov_score`, `ov_nw`, `stk_price` desktop + mobile fold shots: verdict → hero KPI → table eye path intact; plate recession visible (A2); no clipped CJK labels; barometer tick reads against every regime run including overheat (B1a).
4. Open the HTML directly: no console errors; a held stock shows price line + buy/sell markers + avg-cost step; tab re-entry is instant (B4a) while a hard reload replays the full Morning Open; `prefers-reduced-motion` strips every animation including the new ones; `#qbar` is Tab-reachable and Enter lands on the QQQ 决策台 (B1c); on a phone (or DevTools coarse-pointer emulation) a tap pins the crosshair readout and a second tap outside clears it (B6).
5. Payload check: `ls -l output/portfolio_dashboard.html` vs the 711,297B baseline — cumulative growth across all three phases must stay ≈ +5.5KB (hard ceiling +120KB). Log the per-phase delta in the commit message.

---

# ARCHIVE — ROUNDS 1-2

# Portfolio Dashboard — Design Improvement Plan
**"Graphite Atelier" sharpening pass · synthesized from the 7-axis verified review · 2026-06-09**
**Scope:** `generate.py` HTML template (line 1782+) + `scripts/_audit_shots.py` + `output/audit_shots/_DESIGN_REF.md`. All hard constraints apply to every item: single file, zero view-time network, vanilla JS/CSS/SVG, `node --check` must pass, `.replace("__DATA__")` injection untouched, Chinese-first, Fibonacci-as-reference framing, amber rationed, green/red P&L/direction-semantic only, no hard-coded tickers. **Total added-byte budget across the whole plan: ~14KB against the ≤120KB ceiling** (itemized below).

---

## 1. Scorecard

| Skill axis | Score /10 | Verdict |
|---|---|---|
| Typography | 7.5 | Real embedded identity with disciplined numerals — but declared weights (500/650/700) and math glyphs (≈ ± ≤ ≥) the subsets can't honor silently leak fallback faces mid-string. |
| Cohesive color | 7.5 | Amber rationing (0.065–0.096% coverage) and green/red discipline genuinely hold — yet the flagship semantic moment (tier-1 KPI sign color) is dead CSS, and two parallel palettes have ~50 raw-hex drift sites. |
| Motion | 6.5 | One genuinely orchestrated page load, then near-silence: tab indicator teleports, stock entry hard-cuts, and the most-visited chart never draws in. The lowest axis. |
| Spatial composition | 7.0 | Verdict leads and the ruled KPI ledger reads as one instrument — but the landing screen is ~73% preamble before the roster, and the 净值 tab buries its hero chart. |
| Atmosphere | 7.0 | Principled four-step surface ladder, two-tier borders, disciplined shadows — but the named grain (std ≈1.1 RGB) and bloom are below perceptual threshold. The atmosphere lives in comments, not pixels. |
| Distinctiveness | 7.5 | Ownable detail families everywhere (engraved ticks, run-merged strips, bilingual graphite texture) — but the self-declared signature mark is a 132×6px whisper and tab bodies decay into generic card stacks. |
| Data viz | 7.5 | Terminal-grade chart craft (niceTicks, weight hierarchy, marker design) marred by truth defects: overprinting tables, untruthful legend swatches, an ambiguous year tick, a crosshair dot pinned to the frame. |

**Honest read.** This sits at ~7.2 overall — comfortably above generic-dashboard territory and unusually disciplined in its restraint, which is the hardest axis to fake. The single biggest gap is **execution intensity vs. documented intent**: the identity is fully specified in tokens and comments but shipped at sub-perceptual values (invisible grain, swallowed bevel, whisper-sized signature ribbon, dead KPI sign color), so at arm's length the pixels say "competent dark dashboard," not "Graphite Atelier." The second gap is motion after the load choreography ends — the daily-trader loop (tab → stock → back, dozens of times a day) is the least animated surface on the page. Closing both gaps requires almost no new design, only turning up what already exists and finishing what was started.

---

## 2. What already works — do not regress

- **The embedded type identity actually ships:** three Latin-subset WOFF2 faces as base64 `@font-face` (generate.py:1806–1808, ~19KB, `unicode-range` gated), FOUT-free, zero network. Tabular slashed-zero numerals enforced at body level and per-class (1871, 1875–1877, 2175) so chart axes match table columns.
- **Tokenized type ramp with real adoption** (8 steps, 74 `var()` uses vs 23 stragglers; tracking tokens at 1826) and a written CJK/Latin mixing policy (1847–1852, 2079) — Chinese leads, Latin annotates, numerals testify in mono.
- **Amber is measurably rationed** (≤0.1% pixel coverage per tab) and every instance is meaningful: masthead tick → active tab → verdict rail → equity line. **Green/red appear only on signed money and direction** — zero decorative instances found across six screenshots.
- **The surface ladder renders as real depth** (bg ~rgb(10,10,12) vs panel ~rgb(22,23,26)), with deliberate panel2/bg2 demotion tiers. Depth from luminance, never extra hue.
- **The KPI "ruled ledger"**: one panel, hairline internal rules, hero cell with animated amber tick, demoted tier-2 accounting band (1929–1981) — the opposite of floating stat cards.
- **Three-tier card system in real use** (.t1 / .ref / foldcard + dashed .ref-divider, 2265–2300), proven on the QQQ/TQQQ tab; lazy seg rendering kills the wall-of-cards landing view.
- **Page-load choreography** ("The Morning Open", 2380–2396) on one easing token, armed/de-armed correctly; **exemplary reduced-motion discipline** (everything inside the no-preference block + a force-reset reduce block, 2410–2417). `armDraw`/`getTotalLength` draw-in is correctly wired where it exists and restarts on tab re-entry.
- **Cockpit precision:** sticky offsets derived from a ResizeObserver-measured `--header-h` (4261–4270), disciplined 1480px ultrawide sheet, correct mobile stacking at true 390px, 44pt targets.
- **Chart craft:** shared 1/2/5 tick engine (2815–2823), live-amber-2.3px vs dashed-grey-benchmark weight hierarchy, colorblind-safe filled/hollow trade markers with sqrt-area sizing and same-day merging, run-merged regime strips (no corduroy), halo'd collision-relaxed end labels, refined tooltips with live 超额 delta, Chinese-language empty states.
- **Micro-detail hygiene:** amber `::selection`, styled scrollbars, amber `:focus-visible` everywhere, custom select chevrons, the grey hover-answering title tick (2122–2127).
- **The run-merged state-strip motif** recurring across QQQ ribbon / weather strip / 组合动能 / fib strips — a real proto-signature no library produces.

---

## 3. The aesthetic thesis

**Graphite Atelier, stated as a rule you can lint:** this is *one sheet of engraved graphite ledger paper under a single lamp*. Everything that is **the market** is grey — a ladder of greys with real tooth and recessed glass where data lives. Everything that is **your money** is the one amber thread — masthead tick, live equity line, the today-mark. Green and red exist only as **the sign of money or the direction of risk**, never as decoration. Motion behaves like a drafting instrument: lines are *drawn*, ticks are *stamped*, nothing bounces. The page must read this way at arm's length, not just in the stylesheet comments — which means every value currently shipped below perceptual threshold gets turned up to "just perceptible," and every rule gets an enforcement mechanism.

**THE signature move — the Regime Barometer.** Promote the run-merged QQQ 60-day state strip (the code's own comment already calls it "the signature mark," generate.py:3222) to a full-width 6px weather band on the bottom edge of the sticky masthead, with a 2px amber "today" tick at its right terminus — present on all 11 overview tabs and 4 detail tabs, both widths, at every scroll position. It is chosen because it is the only candidate that is simultaneously: the product's core question ("should I trade today?") answered permanently; a reuse of an existing renderer and palette (zero new colors, <1KB); and the thing a visitor would describe afterward ("the graphite terminal with the weather band"). The other bold moves are dispositioned: **pen-plotter draw-in** → adopted as the motion signature (Phase B); **instrument-glass chart plates** → adopted as the atmosphere keystone (Phase C); **palette single-sourcing + lint** → adopted as enforcement (Phase B); **Morning Sheet fusion** → partially adopted as band compression (Phase B), full fusion cut; **ghost folio numerals** and the **paintFrame axis-kit refactor** → cut (see Non-goals).

---

## 4. Phased plan

> Effort: S < 1h, M = half-day. Byte costs are deltas to the emitted HTML; "~0" = net-zero or negative.

### Phase A — Quick wins (all S, immediate payoff)

**A1. Fix the audit harness before anything else** — closes SC-3, DV-3 (both P1)
`scripts/_audit_shots.py:22` (WIDTHS) and `shoot()` (43–55). Chrome `--headless=new` clamps window width to 500px, so every `*_mobile.png` is the left 390px crop of a 500px layout — mobile QA has been blind. Fix: render the dashboard in a `width:390px` iframe hosted in a ≥500px window and screenshot/crop the iframe box (proven to work); write `window.innerWidth` into the DOM before the shot and **fail loudly** if ≠390. Add one real-fold capture per tab (1440×900 and 390×844) — the 5200/6400px-tall viewports balloon every vh cap (`.list` 74vh at 2065, roster `100vh-300px` at 2270) and misrepresent rhythm. Dev-only, 0 bytes. **Re-shoot all mobile baselines before acting on any mobile judgment.**

**A2. Resurrect tier-1 KPI sign color** — closes COLOR-1 (P0)
After `generate.py:1957` add: `.kpi .v.pos{color:var(--green)} .kpi .v.neg{color:var(--red)}`. The markup already emits `class="v pos|neg"` (2604) but `.kpi .v` (1954–1956, specificity 0-2-0) beats `.pos/.neg` (2156). Effect: the three signed hero numbers — the user's first morning question — fire green/red against the graphite field, restoring the intended flagship semantic moment. ~50 bytes.

**A3. Repair the two broken table renders** — closes DV-1, DV-2 (both P1)
(a) `.frow .fval` at 2325: `width:auto;min-width:84px;flex:0 0 auto;white-space:nowrap;text-align:right`; give `.fst` (2326) `flex:0 0 auto` — kills the `$3,366.440%` overprint in 盈亏贡献 (rows built 3050–3052). Verify shared consumers: bridge 3180–3181, rebal 3674, fib ranking 3488, journal 3729/3748/3769.
(b) bridgeCard `leg()` at 3178 **and** the 合计盈亏 subtotal at 3181: emit `<span class="fsym" style="width:auto;min-width:150px">` (precedent at 3130) — kills the 5-line vertical CJK label stack in 真金白银桥. ~0 bytes.

**A4. Stop mid-token wraps in mobile decision cards** — closes T4 (P1)
In the `.smc-mid` template at 3097 only: wrap each metric token (`动能 +2.6`, `RSI 49.1`, and `sigTxt` with its `金叉 06-29` date) in `<span style="white-space:nowrap">`. Do **not** touch the desktop cell at 3092 (already protected by `th,td{white-space:nowrap}` at 2238). Do **not** use U+2011 — it's outside the embedded `unicode-range` and would render a fallback face mid-date. ~200 bytes.

**A5. Complete the embedded type ramp** — closes T2 (P1), T5 (P2)
One subsetting session: (a) embed an **IBM Plex Mono 500** subset (same `unicode-range`, new `@font-face` after 1808) so the six `font-weight:500` mono surfaces (1917, 1955, 2089, 2154, 2325, 2338) render their designed Medium instead of silently downgrading to 400 — the hero KPI included; (b) re-subset Plex Mono 400/600 (and the new 500) adding **U+00B1, U+2248, U+2264–2265** and extend the `unicode-range` descriptors at 1807–1808 — fixes mixed-face ≈/± mid-numeral-string (3033–3035, 3136, 3670); (c) normalize unreachable weights: SVG `font-weight="700"`→`600` at 2924/2960/2990, `.flag-head` 650→600 at 2142. **Byte cost: ~12KB base64** (the plan's single largest addition).

**A6. One verdict rail, not two** — closes DIST-4 (P2)
In `insightBanner()` (3243): emit `<div class="card ib" style="border-left-color:${heroTone}">` and delete the inner hero's inline `border-left` at 3230. Exactly one 3px rail carries the day's tone; net amber reduction. ~0 bytes. (Sets up B4.)

**A7. Demote the stale-banner border one amber tier** — closes COLOR-4 (P2)
At 2578, split the branches: stale → `var(--accent-line)` (the 0.32-alpha token, 1838); **keep `fetchOK===false` red solid** (it's a data-integrity safeguard, 2580). At 2582: `border-radius:8px`→`var(--r-card)`. Leave the `--amber-line` token untouched (UIUX VIS-1, shipped). The verdict rail becomes the top amber element again. ~0 bytes.

**A8. Tab indicator blooms instead of teleporting** — closes MOT-3 (P1)
Inside the no-preference block: `.seg-rail button.on::after,.tabs button.on::after{animation:tickIn .22s var(--ease);transform-origin:center}` (reuses keyframes at 2374; the pseudo-element is recreated on toggle so it replays). Add the same selector with `animation:none` to the reduce block (2410–2417). The highest-frequency interaction on the page gains its instrument moment. ~150 bytes.

**A9. Trade markers acknowledge the cursor** — closes MOT-4 (P2)
No-preference block: `.mk{transform-box:fill-box;transform-origin:center;transition:transform .15s var(--ease),fill-opacity .15s var(--ease)} .mk:hover{transform:scale(1.3);fill-opacity:1}`; reduce block: `.mk{transition:none}`. Markers emitted at 2887 already carry the class. ~170 bytes.

**A10. Make the grain perceptible** — closes BG-1 (P1), half of DIST-3
(a) `body::after` opacity `.045`→`.06` at 1799; (b) inside the `--grain` data-URI at 1854, insert `<feComponentTransfer><feFuncA type="gamma" exponent="1.6"/></feComponentTransfer>` after `feTurbulence` — shapes alpha contrast so it reads as paper tooth, not a uniform brightness lift. Target: panel std ≈3–4 RGB at 1x, still calm. Grain rides z:30 above text (1793) — re-verify contrast on screenshots. ~120 bytes.

**A11. Chart-family truth pass** — closes DV-4 (P1), DV-5, DV-6, DV-7, DV-8 (P2s)
- **Year tick:** `monthTicks` at 2829 — January emits `String(d.getFullYear())+'/1'` (`2026/1`, ~39 units at 11px mono, fits the ~80px/43-unit month gap). Optionally add year to the five tooltip date headers (2873, 2926, 2963, 2997, 3914). Note: deliberately overturns the shipped B7 `26/1` convention — update its validation reference.
- **Truthful legends:** add `.legend i.lnt{width:16px;height:0;border-top:2px dotted currentColor;background:none!important;border-radius:0}` beside 2170; 纳斯达克 swatch at 3416 → `lnt`; risk-tab square chips at 3592/3596 → `.ln/.lnd` matching their strokes; QQQ legend at 3365 → `lnd` for the dashed ema34/55.
- **Crosshair rides the data:** store `ys:[…yc(v)…]` for the primary series in every CHARTREG assignment (2873, 2926, 2963, 2997, 3914); in `place()` at 4104, `cy = reg.ys[lo] ?? ln.getAttribute('y1')`.
- **Month gridlines everywhere:** mirror chart()'s vertical hairline (2856) in svgLines (2909), qqqStrategyChart (2952), nwChart (2982), fibChart (3887) — `stroke=C.hair`, strip-aware bottoms (`H-mB-stripH` where strips exist).
- **Legible guide labels:** at 2901, split line stroke from label fill via optional `g.labelColor` — semantic guides get `C.green/C.red` at `fill-opacity:.75` (≥3:1 at 10px); callers at 3526/3528/3946/3948–3949.
All ~0 bytes net. Re-run `node --check` after.

**A12. Sync the canon** — closes COLOR-5 (P2)
Update `output/audit_shots/_DESIGN_REF.md` token block to shipped values (bg `#060608`, bg2 `#0E1013`, panel `#121316`, panel2 `#1B1E24`, faint `#7E848E`) and add the JS chart const `C` (2810–2812) so the JS-side palette is canon too. Doc-only.

### Phase B — The identity pass

**B1. THE SIGNATURE: dock the Regime Barometer on the masthead** — closes DIST-1 (P1); the chosen bold move
Render the run-merged QQQ 60D strip (reuse the rect loop at 3224–3228) as a full-bleed 6px band on the sticky masthead's bottom edge, stretched via `preserveAspectRatio="none"`, with a 2px **amber** today-tick at the right terminus and the existing title-attr legend (绿=多头 · 琥珀=过热 · 红=破21 · 灰=转换). **Do not hand-bump `--header-h` at 1828** — it's a fallback guess; the ResizeObserver (4261–4270) measures real height, so either let the band add natural header height (all sticky offsets auto-recompute) or absolutely position it on the header's bottom edge. Guard on the existing `qSer.length>5` gate (3225) — no data, no band. Keep the hero-row ribbon as the labeled echo. Green/red stay regime-direction-semantic; amber stays the lone live marker. Effort M, **<1KB**.

**B2. Pen-plotter draw-in as the motion signature** — closes MOT-1 (P1); motion bold move
(a) Widen the CSS selector at 2405 `svg polyline.draw`→`svg .draw` and `armDraw` at 2807 `polyline.draw`→`.draw` (root-svg `class="xh draw"` at 2999 is harmlessly swallowed by the try/catch and never matched by the descendant selector); (b) add `class="draw"` to the stock price polyline at 2862 and a `.draw2` variant (`animation-delay:.5s`) on the amber avg-cost step path at 2868 — *market draws first, then your position follows*; (c) pass `draw:1` on lead series only: 3593 (drawdown), 3526/3945 (momentum), optional draw arg in qqqStrategyChart's `line()` at 2954. Dashed references stay static so hierarchy reads. Reduced-motion safe for free (lives in the no-preference block). Effort S, ~0 bytes.

**B3. Stock entry settles instead of cutting** — closes MOT-2 (P1)
In `renderDetail`, line 3971: `<div class="seg segin" data-seg="price">`; in `renderOverview`'s skeleton (3454), emit `segin` on the initialSeg panel (optionally gated behind `body.done` to avoid doubling the load stagger). The existing `.seg.segin:not([hidden])` rule (2399) and reduce-block reset (2414) do the rest. Update the now-stale comment at 2397–2398. Combined with B2: panel settles .20s, grey line draws — a real arrival. Effort S, ~0 bytes.

**B4. The verdict gets stamped** — closes MOT-6 (P2)
Building on A6 (tone now on the outer `.card.ib` rail): expose the tone as an inline `--hero-tone` custom property at 3243, then no-preference block: `body.ready .card.ib{border-left-color:transparent;animation:heroEdge .01s linear .55s forwards}` + `@keyframes heroEdge{to{border-left-color:var(--hero-tone)}}`. The severity rail snaps on *after* the sentence lands (.46s riseIn + delay) — the judgment reads as stamped. Reduce-block reset included. Timing only, never hue. Effort M, ~150 bytes.

**B5. Single-source the palette and lock it** — closes COLOR-3 (P1); color bold move
(a) Derive the JS `C` const from `getComputedStyle(document.documentElement)` at boot (or keep literals but assert sync) so one `:root` edit moves every surface; (b) sweep the drift: inline `${c}66`/`#E8B33966` chip borders at 3079/3135/3300/3323 → the existing `--chip-bd-*` convention (1842); nw legend swatch 3408 → `C.grey`; neutral momentum unified on `C.neut` (3496 vs 2976); fetchwarn error tint 2579 derived from `--red`; bottom-strip 黄=转换 legends 3523/3941 → the actual painted `#B89030`; delete or wire the dead `--accent-ref`/`--mut2` (1836, 1841); (c) **add a ~10-line palette lint to `scripts/_audit_shots.py`** that greps the emitted HTML for hex literals outside `:root`/`C` and fails the audit on new drift — the one rule becomes enforceable. Effort M, ~0 bytes (likely negative).

**B6. Restore the amber chart grammar on the fib panels** — closes COLOR-2 (P1)
In fibChart (3898–3899): subject price line → `C.subj #D9DCE1` at ~2px (matching the QQQ close-line convention, 2934); **shift the amber ladder down one tier** — EMA5 → `#C99A3A`, EMA8 → `C.ref #B89030` (avoids the EMA5/EMA8 collision the original rec had). Update **all** echo sites: legends 3519–3520 *and* the per-stock duplicates 3937–3938, the fibChart tooltip rows at 3914, oscillators at 3526 (may stay amber only if relabeled as portfolio-derived) and 3945 (→ `C.e8w`). "Indicators never claim the live accent" becomes true everywhere. Effort S, ~0 bytes. `node --check` after.

**B7. Compress the front page: six bands → four** — closes SC-1 (P1), SC-2 (P1); the adopted slice of the Morning Sheet
(a) At ≥981px, merge the **overview** viewbar (1996) into the seg-rail band — breadcrumb left, seg buttons right, one 44px bar (~55px saved); decide the 最近查看 chips' home (2753–2757: into the merged bar, or desktop-drop); keep `viewBarStock` (2760) separate — it carries the back button. (b) Move the 今日先看 mover chips inline into `.ib-hero` **before** `${ribbon}` (3230; it's right-aligned via margin-left:auto — insert after, and the row wraps, saving nothing), delete the separate `.ib-row` at 3245 (~36px saved). (c) Demote **only the amber stale state** of fetchwarn to a compact mono masthead chip (`⏱ 数据 −1d · sync.py`, full sentence in title, keep `role=alert`); the red fetch-failure band stays full-width — it's a documented safeguard (2580). Target: 决策一览 roster top ≤ ~480px at 1440×900 (5 → ~11 rows above the fold). Effort M, ~0 bytes.

**B8. 净值 tab opens with its hero** — closes SC-4 (P1)
Reorder `PANEL_RENDERERS_OV.nw` (3403–3411): `wholeAccountCard()` stays **first** (it answers the tab's literal promise, 3457), then 组合总览 badges + nwChart card, then fold the two option cards below a `.ref-divider` — **refactor `optionsExposureCard` (3124–3142) and `optionsSpreadLedgerCard` (3143–3156) to return body-only HTML** before wrapping in `foldCard()` (3258), surfacing the gross-exposure figure in the fold summary so the risk cue isn't hidden; diagnostics last. The tab gains the open-with-a-chart contrast qt already has, and the draw-in (B2) plays near the fold. Effort M, ~0 bytes.

**B9. Sharpen the seg-rail out of admin-nav genericness** — closes DIST-2 (P1)
Rename the 风险 tab → 波动贡献 (or the group → 账户) to kill the word duplication; shorten 净值 · 全账户 → 净值·账户, 斐波那契·技术 → 技术·节奏; cut button padding `11px 16px`→`11px 12px` (2353) **and retune the active-underline insets** (left/right:16px at 2359–2362) so all 11 tabs fit at 1440px without silent clipping (rail is overflow-x:auto with hidden scrollbars, 2347–2349); restyle `.seg-grp` (2363–2369) as the tick-family glyph (3px grey vertical tick + 10px label, like h1::before at 1906–1911) so groups read as architecture. Chinese-first kept. Effort S, ~0 bytes.

**B10. Bilingual masthead lockup** — closes DIST-7 (P2)
Add a one-span Space Grotesk shoulder line to the h1 lockup (markup 2528, CSS block 1900–1911): `PORTFOLIO TIMELINE`, 10px, letter-spacing .14em, `var(--faint)`, beside the amber tick. The embedded subset already covers it (U+0020–007E) — the display face finally gets a permanent stage at **zero font bytes**; CJK h1 stays primary. Effort S, ~60 bytes.

### Phase C — Polish & atmosphere

**C1. Engrave the instrument glass** — closes BG-2 (P1); atmosphere bold move
Add `bg2:'#0E1013'` to `C` (2812; keep in sync with 1832 — or free via B5's derivation) and **prepend** one backdrop rect `<rect x=mL y=mT … fill="${C.bg2}"/>` in all five builders, before gridlines so hairlines and the existing washes (2860, 2986) sit on it; keep the stroked frame on top. **Use each builder's own geometry:** plot height is `H-mT-mB` in chart/svgLines (2858/2906) but `H-mT-mB-stripH` in qqqStrategyChart/nwChart/fibChart (2953/2983/3888) — a fixed h would overlap the state strips. Every chart on all 15 tabs becomes a recessed plate: papered card, recessed glass, amber line. Effort M, ~120 bytes. Reshoot stk_price/ov_nw/ov_pfib.

**C2. Ground the bloom; trace the sheet** — closes BG-3 (P2) + the rest of DIST-3 (P1)
Pin the bloom's vertical anchor in px at 1798 (`… -6%` → `… -60px`; keep the horizontal `min()` clamp — non-goal #5 of the prior plan protected it); brighten the core one step (`#181B21`→`#1C2026`) or add a weaker second bloom as `background-image` on the translucent masthead (1894–1896) so the top chrome carries the lamplight; append a faint counter-bloom bottom-left (`radial 1200px 600px at 10% 110%, #101216 → transparent`) to `body::before` so the bottom half of every tab stops being featureless void; add fixed 1px `var(--hair)` hairlines at the 1480px sheet boundaries (1887) for ≥1500px viewports. All static. Effort S, ~250 bytes.

**C3. Bevel: raise or delete** — closes BG-4 (P2)
`.card` inset top-light at 2112: `.02`→`.045`, match the masthead inset (1898) to the same value. If still invisible under the brighter `--line` border at 1x — delete both; invisible detail is stylesheet noise, not restraint. Effort S, 0 bytes.

**C4. Rail scroll-edge fade** — closes BG-5 (P2)
`.list` (2065): `mask-image:linear-gradient(to bottom,#000 calc(100% - 28px),transparent)` + `-webkit-` prefix. Caveat: a short filtered list still fades its last row — accept, or gate behind a class toggled in `renderHoldingsList`. Effort S, ~90 bytes.

**C5. Extend the tick-answers-cursor hover motif** — closes MOT-5 (P2), with verifier corrections
(a) KPI hero tick: switch the tickIn fill-mode at 1977 from `forwards` to `backwards` and set the resting `transform:scaleX(1)` in a `body.ready` base rule (forwards-fill otherwise defeats any hover transform); add `transition:transform .2s var(--ease)` to the ::after (1971–1975); hover → `scaleX(1.6)`. (b) Row hover: do **not** animate `border-left-width` (1px layout shift) — add `box-shadow:inset 1px 0 0 var(--line)` on `.row:hover` instead. Still grey, still calm. Effort S, ~150 bytes.

**C6. Fold rows become a scannable index** — closes SC-6 (P2)
Extend `foldCard()`'s signature (3258) with an optional right-aligned chip param (summary is already flex, 2288 — `margin-left:auto`), then thread one **neutral grey mono** value per call site (counts, dollar figures: 执行地图 "4 条", 当前仓位 "$354"; sites at 3306, 3315, 3328, 3336, 3342, 3346, 3349, 3369, 3371, 3375). **No green/amber pass-fail chips** in always-visible summaries. Effort S, ~300 bytes.

**C7. Reference zones get controlled density** — closes DIST-6 (P2)
At ≥1200px, two-column the collapsed folds: grid on `.seg[data-seg='qt']`/`.seg[data-seg='struct']` with non-fold children (`.card`, `.ref-divider`) spanning `grid-column:1/-1` (there is no fold container — folds are direct seg children, 3369–3381 — or wrap them in `qqqTqqqTab()`). Strengthen `.t1`'s top rule with a **brighter neutral** — `2px solid color-mix(in srgb,var(--mut) 35%,var(--line))` — **never** `var(--accent-line)` (amber is rationed; the prior plan specified `--line` deliberately). Each tab: one primary decision object over a clearly subordinate reference field. Effort M, ~200 bytes.

**C8. Calm the buy-campaign clouds; finish the fib end label** — closes DIST-5 (P2), DV-9 (P2)
(a) In the marker loop (2879–2887): when `Object.keys(mgrp).length>12`, cap r at 7 and drop BUY fill-opacity to 0.18 (note: flattens size encoding at coarse-pointer RMIN=7 — accepted for calm). (b) Extend the merge key at 2878 to cluster same-side trades within **≤2 trading sessions** (day-distance, not fixed viewBox units — desktop/mobile plot widths differ), sized by summed amount; **update the tooltip**: date header at 4082 becomes a range, per-leg dates added to the legs list at 4081. (c) fibChart 3908: render `现价 ${yl(cp)}` (`yl` in scope, 3873–3874) with `C.subj` fill + panel stroke-halo (precedent 2924/2990) — value included, amber stays EMA-owned per B6. Effort M, ~0 bytes.

**C9. Type hygiene close-out** — closes T3 (P2), T6 (P2)
Replace the 7 inline `font-weight:650` caption divs (3525, 3527, 3591, 3595, 3684, 3944, 3947) with `class="cap"` (2144), preserving each site's inline margins; delete the `[style*="font-weight:650"]` normalizer at 2135–2139. Ramp sweep: 9× `11px`→`var(--t-xs)`; add `--t-2xs:10px` to `:root` (1824) for the five 10px sites (2083, 2094, 2303, 2306, 2324); resolve the 4× `14px` (2278, 2281, 2493, 3734) to `--t-base` or `--t-md`; one shared `AXIS_FS` const for the hard-coded SVG font sizes (2852, 2901, 2947, 2990 + ~16 more). Effort S, ~0 bytes.

**C10. (Optional, last) Desktop rail spine** — closes SC-5 (P2)
On the score seg only: collapsible 56px ticker spine (state dot + symbol), microbtn toggle persisted to localStorage (pattern at 2666–2667). Returns ~244px to the roster and adds deliberate asymmetry. Ship only if the post-B7 fold measurements still want the room. Effort M, ~400 bytes.

**Byte ledger:** A5 ~12KB · B1 <1KB · everything else ≤ ~2KB combined · **total ≈ 14–15KB of ~120KB budget.** No CJK faces, no images, no libraries.

---

## 5. Explicit non-goals

1. **Embedding Archivo, or deleting it from the stack** — shipped, documented zero-byte progressive layer (FRONTEND_DESIGN_PLAN B1/A12; comment at 1847–1849). Killed T1 stays killed.
2. **Forcing Space Grotesk onto CJK or measured-data surfaces** (masthead date, period stamps, tickers) — the identity's role separation is deliberate: Chinese leads, Latin annotates, numerals testify in mono. Killed T7 stays killed.
3. **96px ghost folio numerals / ticker monograms** (typography bold move) — decorative ornament against this product's refined-minimalism reading; B1 + B10 deliver signature presence without graphite wallpaper.
4. **The full "Morning Sheet" verdict+KPI grid fusion** — B7 takes the band-compression value; wholesale restructuring of the page's two strongest shipped components in a no-test monolith is bad risk/reward.
5. **The `paintFrame` shared axis-kit refactor** (dataviz bold move) — A11 captures the user-visible drift fixes; rewriting five working builders at once is the riskiest possible change to a file validated only by `node --check`. Revisit only if B5's lint shows recurring drift.
6. **U+2011 non-breaking hyphens** — outside the embedded `unicode-range`; would create the exact mixed-face defect A5 fixes.
7. **Demoting the red fetch-failure band, or touching `--amber-line`** — data-integrity safeguard (2580) and a shipped UIUX VIS-1 decision respectively.
8. **Amber on `.t1` rules, colored pass/fail fold chips, or any new hue/gradient/glow** — amber stays rationed to "your money + you-are-here"; green/red stay the sign of money and direction of risk; depth comes from luminance only.
9. **Scroll-driven or looping animation** — motion stays event-driven (load, reveal, hover, stamp) and fully inside the reduced-motion gates.

---

## 6. Validation protocol

Run after **each phase** (A, B, C):

1. **Generate + gate:** `python3 sync.py --no-fetch` — must print `got N/N tickers` and `OK` for marketValue / unrealized / numHeld. Never trust output past a MISMATCH.
2. **JS integrity:** extract the `<script>` from `output/portfolio_dashboard.html` → `node --check`. Mandatory after every template-literal edit (A11, B1, B4, B6, C1, C8 especially).
3. **Visual audit:** re-run `scripts/_audit_shots.py` — **post-A1 harness only** (true-390 iframe + innerWidth assert + real-fold 1440×900/390×844 shots). Eyeball `ov_score`, `ov_nw`, `stk_price` desktop+mobile minimum. From Phase B on, the palette lint (B5c) must pass.
4. **Payload check:** emitted HTML size vs the 711KB baseline; maintain a running ledger — ceiling 711KB + ~15KB planned (hard cap +120KB). Any item exceeding its stated cost gets re-justified or reverted.
5. **Reduced-motion pass:** toggle OS prefers-reduced-motion (or DevTools emulation) — zero animation fires; every new keyframe/transition has its reduce-block counterpart (A8, A9, B2–B4, C5).
6. **Manual smoke (manual smoke checklist):** open the HTML; no console errors; a held stock shows price line + buy/sell markers + avg-cost step.
7. **Phase-specific acceptance:**
 - **A:** KPI heroes render green/red; no overprint in 盈亏贡献/真金白银桥; hero KPI renders Plex Mono Medium at high zoom; ≈/± render in Plex on badge values; grain visible at 1x (panel std ≈3–4 RGB) with no text-contrast regression; `2026/1` on every cross-year axis; crosshair dot rides the line; mobile decision cards never split `金叉 06-29`.
 - **B:** barometer present on all 15 tabs at both widths and absent when QQQ data is unavailable; sticky offsets intact after header growth (no slit under the rail — ResizeObserver check); draw-in replays on tab re-entry, dashed references static; roster top ≤ ~480px at 1440×900; palette lint green; fib EMA5/EMA8 visually distinct.
 - **C:** recessed plates on all five chart types with strips un-overlapped; bloom visible at real 900px viewport top band; fold chips neutral grey only; bevel either perceptible or gone.
8. **Doc close-out:** `_DESIGN_REF.md` updated (A12, plus C-const and any phase-changed tokens) so the next audit judges against current canon.

---

# ARCHIVE — ROUND 1 (implemented 2026-06-09/10)

# Design Improvement Plan — "Graphite Atelier", Sharpened
**Portfolio Dashboard · synthesized from the 7-dimension verified design review · 2026-06-09**
**Scope:** `generate.py` `HTML_TEMPLATE` (line 1782+) → `output/portfolio_dashboard.html` (current baseline **738,554 B**; June-8 review baseline 711,297 B). All edits live in one file. Reference identity: `output/audit_shots/_DESIGN_REF.md`. The shipped June-8 usability pass (`UIUX_REVIEW.md`) is prior art — nothing below duplicates it.

---

## 1. Scorecard

| Skill axis | Review dim | Score /10 | One-line verdict |
|---|---|---:|---|
| Distinctive typography | typography | **4.5** | The declared identity never renders: display == body, the numeral face is browser-dependent — the type system is a comment in the source, not a fact on screen. |
| Cohesive dominant color | color | **7.0** | Token discipline and amber rationing genuinely hold on desktop chrome; the grammar leaks in chop regimes (转换中 floods amber) and on two charts. |
| Orchestrated motion | motion | **4.5** | One calm easing curve and real reduced-motion gating — but the signature chart draw-in is dead code and the most frequent interaction (tab/stock switch) is a hard cut. |
| Intentional spatial composition | spatial | **6.0** | The ruled KPI ledger is the best-composed band in the file; the page has no typographic apex, and an identical ~900px options tail flattens all 11 tabs. |
| Atmospheric backgrounds | atmosphere | **5.5** | "Engraved paper" exists at homeopathic values — grain sits at z-index:-1 behind fully opaque panels; chart interiors measure stddev 0.00. |
| Anti-slop distinctiveness | distinctiveness | **6.0** | The bilingual ledger voice and editorial content design are real and ownable; the two things a visitor would remember (the type, the verdict) are fiction and whisper respectively. |
| (Bold direction, exercised through data) | dataviz | **5.5** | Terminal-grade shared crosshair/tooltip layer — undermined by arbitrary axis values, clipped date labels, and charts that render at ~5px text on a phone. |

**Honest read.** This is a deliberately designed instrument with unusually good bones — token sheet, component grammar, a11y gating, editorial voice — sitting well above generic-dashboard slop in *intent* but landing at roughly a 5.5/10 average in *rendered fact*. The single biggest gap is that the identity's strongest claims are unexecuted: zero `@font-face` in the output, the hero draw-in animation matches zero elements, the verdict sentence inherits 13px body text, and the atmosphere layer is mathematically invisible. The bar for this product is refined minimalism executed with precision — which means the plan is overwhelmingly *shipping what was already specified*, plus one signature composition, not adding decoration.

---

## 2. What already works — do not regress

- **The KPI ledger strip** (generate.py:1899–1956): one ruled graphite panel, hairline internal dividers, 26px hero cell with the animated amber tick, demoted tier-2 accounting row on `--bg2`. The best band on the page and the template for everything else.
- **Numeric routing discipline**: every major numeric surface is `--f-mono` + `tabular-nums` + `slashed-zero` (`td` 2198, `.kpi .v` 1933, `.badge .v` 2127, `.kl-v` 1944, all SVG text 2145; root features at 1851–1852).
- **Swiss micro-label grammar**: 10–11px uppercase letterspaced muted labels (`th` 2194, `.kpi .l` 1925, `.cap` 2117, `.badge .l` 2124) — credible print-ledger voice even in fallback faces.
- **Color token discipline** (1817–1831): documented roles, legacy aliases that block stray hues, single chip-border convention; measured amber share 0.22–0.30% per desktop tab; green/red strictly semantic; the cmp chart's "one amber line vs two dashed greys" is a genuine signature moment (3276).
- **Shape-redundant trade markers**: SELL hollow ring vs BUY filled disc (2778–2779) — colorblind-safe by design.
- **Unified chart interaction layer**: `CHARTREG` + one crosshair/tooltip system (3936–3956), amber hairline + dot, ruled delta row, coarse-pointer thickening (2178).
- **Motion a11y discipline**: the whole reveal block inside `prefers-reduced-motion: no-preference` with an explicit reduce-side neutralizer (2323–2349); the one-shot `body.ready:not(.done)` stagger guard (2335, 4105).
- **Two-tier rule system** (`--line` vs `--hair`) and the shadow policy split "printed ledger vs glass chrome" (2087 `box-shadow:none` on static cards; `--sh-lift` only on floating chrome).
- **Editorial content design**: 今日要点 one-sentence verdict + severity rail, QQQ 日线天气图 weather metaphor, 真金白银桥, 怎么读 explainers, honesty boxes — authored ideas, not boilerplate.
- **Bilingual texture**: Chinese-first headings with uppercase Latin micro-captions; the masthead metadata line reads like an engraved plate.
- **Empty/sparse-data guards** on every chart builder (2737–2740, 2816, 3732, 3772).
- **Registration-tick family** (masthead 1884–1887, hero KPI underline 1949–1956, ctx-tick 2175, card tick 2097–2100) — sharpen it, never replace it.

---

## 3. The aesthetic thesis

**Graphite Atelier, stated precisely: an engraved graphite broadsheet that prints one trader's verdict every morning.** One ground (a perceptible graphite surface ladder under real film grain), one display voice (Space Grotesk for what the page *says*), one numeral voice (IBM Plex Mono for what the page *measures* — tickers included, because tickers are data keys), and one accent that means exactly one thing: **full amber `#E8B339` = the live thing you own, right now**; muted amber `#B89030` = reference; grey = the world. Chinese leads, Latin annotates, numerals testify. Motion is a single morning power-up and the quiet settling of instruments — never a casino.

**THE signature move — "The Morning Front Page."** Synthesized from the distinctiveness and spatial bold moves, enabled by the typography one, choreographed by the motion one: the masthead → verdict → KPI ledger stack becomes one composed instrument. The 今日要点 verdict sentence is set at display scale in real Space Grotesk — the page's only typographic apex — carrying a ~60-day QQQ regime mini-ribbon, sharing the ledger's gutter and hairline grammar, and rising first in the load choreography while the amber net-worth line draws itself in behind it. This is the right signature because it is the one surface the user sees *every single day*, it fuses the page's best-executed element (the ledger) with its weakest moment (the 13px verdict), and it costs ~1.5KB and zero new color.

Disposition of the other bold moves: **typography** (ship the fonts) → Phase B1, the enabler — the signature is impossible without it. **Motion** ("The Morning Open") → split across A1/A7/A8/B10 — it is the choreography of the signature, not a separate move. **Color** (amber-means-one-thing) → A3 + B5, with its per-stock price-line recoloring *cut* (see Non-goals — the grey line is a documented, self-consistent convention). **Atmosphere** (lift the grain / engraved plates) → A2 + C1. **Dataviz** (`axisFrame()` kit) → B7 — kept in full as the engineering spine of chart craft, but it is invisible-when-right infrastructure, not a signature.

---

## 4. Phased plan

**Byte ledger for the whole plan:** fonts +50–60KB (B1) · regime ribbon + verdict CSS +~1.5KB (B2) · misc CSS/JS +~3KB · chart-kit refactor net **negative** (deletes ~80 lines of repeated frame code). **Total ≤ ~65KB**, well inside the ≤120KB budget → final file ≈ 790–805KB.

### Phase A — Quick wins (each S, <1h, immediate visual payoff)

**A1. Resurrect the dead chart draw-in** — *MOT-1 (P0); motion*
The CSS at 2341 (`body.ready svg polyline.draw`) matches nothing; `--len` is never set. (1) Add `class="draw"` to the nwChart hero polyline at **2857**; (2) in `svgLines`, support a per-def `draw` flag emitting `class="draw"` on the polyline at **2805**, set `draw:1` on the amber `ret` def at **3276**; (3) at the end of `ensureOvPanel` (after **3298**) and after `renderDetail`'s innerHTML (**3821**): `panel.querySelectorAll('polyline.draw').forEach(p=>{try{p.style.setProperty('--len',Math.ceil(p.getTotalLength()))}catch(e){}})`. hidden→shown restarts the animation, so every tab entry replays the 1.15s draw free. Reduced-motion already covered. Effect: the page's intended hero motion renders for the first time ever. **0 bytes.**

**A2. Lift the atmosphere above the surfaces** — *BG-1 (P0) + SPC-8; atmosphere*
At **1793–1796**: (1) `body::after` grain `z-index:-1 → 30` (above panels and z:20 header, below z:40 tooltip / z:99 skip; `pointer-events:none` already set), opacity `.035 → .045`; (2) bloom → `radial-gradient(1400px 720px at min(85%, calc(50% + 450px)) -6%, #181B21 0%, rgba(11,12,14,0) 60%)` — the `min()` keeps current placement ≤~1400px viewports and pins the bloom to the 1480px sheet's right shoulder on ultrawide (do NOT use plain `calc()`, it regresses mid-width); (3) hoist the noise data-URI to a `--grain` token. Effect: grain finally textures the opaque cards/charts; a perceptible graphite sky clears the masthead. **<100 bytes.**

**A3. Demote 转换中/mixed amber to the reference tier** — *COLOR-1 + DIST-2 (P1); color, distinctiveness*
`FIBCOL.mixed '#E8B339' → '#B89030'` (**3353**); `momColor` neutral-band return → `'#B89030'` (**3355**); watch-level bias chips at **2946** → color `'#B89030'`, border `'#B8903066'`; base `<summary>` color `var(--accent) → var(--mut)` at **2246**, keep the amber ▸ marker (2248). Effect: in chop regimes the default tab stops drowning in accent; pure amber returns to singular cues (active tab, hero verdict, live line, masthead tick). Mobile amber share should drop from ~0.83% toward desktop's ~0.25%. **0 bytes.**

**A4. Make the QQQ chart legend/tooltip tell the truth** — *COLOR-3 (P1); color*
Define `const QTC={close:'#D9DCE1',ema8:'#E8B339',ema21:'#AEB4BE',ema34:'#888D96',ema55:'#4B4F58'}` next to `qqqStrategyChart` and reference it from the `line()` calls (**2830**), end-labels (**2834**), the on-card legend (**3224** — currently lies: #B6BAC1/#5F6168), and tooltip rows (**2837**). (B5 later retunes `ema8` inside this one const.) Effect: legend chips match plotted strokes; trust in the grey-coding restored. **~0 bytes.**

**A5. Un-clip the x-axis date labels on three charts** — *DV-01 (P0); dataviz*
`stripY` is H-derived, so bump **H and mB together** (keeps stripY and the `yc()` plot height unchanged): nwChart **2842** H 330→346, mB 40→56; fibChart **3734** H 400→414, mB 46→60; qqqStrategyChart **2817** H 390→404, mB 46→60. Verify the full glyph height of "6/9" renders below the strip. **0 bytes.**

**A6. Mono the mobile decision-card mid line** — *TYPO-4 (P1); typography*
Add `font-family:var(--f-mono)` to `.smc-mid` at **2227** (CJK 仓位/动能/金叉 falls through per-character — precedent: `.badge .v`). Effect: the most-glanced phone surface stops mixing proportional and mono digits in one card. **1 line.**

**A7. Fix the load choreography order + stagger micro-bugs** — *MOT-2 + MOT-6 (P1/P2); motion*
After **2333** add: `body.ready #insight .card{opacity:0;animation:riseIn .46s var(--ease) .02s forwards}` and `body.ready .kpi-ledger{opacity:0;animation:riseIn .4s var(--ease) .30s forwards}`. Reduce block (**2347**): `body.ready #insight .card,body.ready .kpi-ledger{opacity:1;animation:none}`. Give the base `.right .card` rule (**2335**) `animation-delay:.32s` so 5th+ cards stop rising before card 1; delete the dead `.kpi:nth-child(5)–(8)` rules (**2329–2332**). Effect: verdict leads, KPIs follow, ledger settles — information-priority order. **~0 bytes.**

**A8. Panel-entry settle on tab switches** — *MOT-3 (P1); motion*
Next to 2318–2321 add `@keyframes segIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}` and `body.done .seg:not([hidden]){animation:segIn .20s var(--ease)}` (`body.done` scoping avoids double-animating the load stagger; display toggle restarts it per entry). Reduce block: `.seg{animation:none!important}`. Effect: the single most frequent interaction stops being a hard cut; with A1, chart tabs open with the line drawing in. **~0 bytes.**

**A9. Unify the left gutter and the 1480px cap** — *SPC-4 (P1); spatial*
`#insight` → `margin:18px var(--s7) 0` (**2180**); `.wrap` → `padding:16px var(--s7) 44px` (**1961**); `#fetchwarn` inline → `margin:10px var(--s7) 0` (**2514**); extend the cap at **1865** to `header,.kpis,.wrap,#insight,#fetchwarn`. Phone override at 2445 already re-overrides. Effect: one ruled left edge at x=32 down the whole band stack, at every viewport. **0 bytes.**

**A10. Close the sticky-header seam** — *SPC-5 (P1); spatial*
Three values claim one masthead height (84 declared at **1814**, 76 used at **1970**/**2288**, ~52 real). Desktop has zero `var(--header-h)` consumers (only the ≤560px block uses it, re-set to 48px at 2399), so: set desktop `--header-h` to the real height (or reuse the `--viewbar-h` ResizeObserver pattern at **4106–4115**), then `.left{top:calc(var(--header-h) - 8px)}` and `.seg-rail{top:var(--header-h)}`; retune `.ctx` top (**2173**) relative. Effect: content stops streaming through a ~24px slit; the sticky stack reads as one fused panel. **0 bytes.**

**A11. Fix the nwChart end-label collision** — *DV-05 (P1); dataviz*
`mR 22→64` (**2842**, coordinate with A5's edit to the same line); label at `x=xs(last)+6`, drop `text-anchor:end` (**2859**); add halo `stroke="#121316" stroke-width="3" paint-order="stroke"`; same halo on `svgLines` end labels (**2807**). **~0 bytes.**

**A12. Latin-first font-stack reorder (zero-byte fallback layer under B1)** — *TYPO-2 (P1); typography*
At **1833–1834**: `--f-disp:"Space Grotesk","Avenir Next","Noto Sans SC","PingFang SC",ui-sans-serif,system-ui,sans-serif`; `--f-ui:"Archivo",-apple-system,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC",sans-serif`. CJK still resolves per-character through the tail. Effect: Latin stops rendering in PingFang's generic Latin; a visible display/body pairing exists even before fonts ship. **0 bytes.**

**A13. Neutralize the onboarding strip** — *COLOR-6 (P2); color*
At **3854–3856**: container → `background:var(--panel);border:1px solid var(--line)`; keep only the leading 从这里开始 amber; restyle the 7 `.ib-lk` links per the grey `.note-lk` convention (**2357–2358**: muted, dotted underline, amber on hover). Effect: the loudest amber block on a fresh load stops outshouting the verdict it sits above. **0 bytes.**

**A14. One-liner batch** — *BG-4, BG-6, MOT-5, TYPO-7; atmosphere, motion, typography*
(a) `::selection{background:rgba(232,179,57,.25);color:var(--txt)}` near **1859**. (b) Card title tick `var(--line) → #3A3F47` at **2099** (stays grey — honors the "not amber" comment at 2096). (c) Reduced-motion leaks: move the dataFlick rule (**2078**) inside the no-preference block (or add `.row.sel .pnl,.row.sel .meta{animation:none}` to the reduce block); `const PRM=matchMedia('(prefers-reduced-motion: reduce)').matches` near init, then `behavior:PRM?'auto':'smooth'` at **2664**, **2491**, and **4066** (all three sites). (d) `.legacychip` 9.5px→10px at **2251** (mobile already floors it at 11px via 2432). **~0 bytes.**

### Phase B — The identity pass

**B1. Ship the type identity: inline base64-subset WOFF2** — *TYPO-1 + DIST-1 (both P0) + TYPO-6; typography, distinctiveness — THE ENABLER*
The output has 0 `@font-face`; display==body==PingFang, the numeral face differs by browser. Subset with fonttools: `pyftsubset --flavor=woff2 --layout-features+=zero,tnum --unicodes=U+0020-007E,U+2191-2193,U+25B2,U+25BC,U+2192,U+2212,U+2013-2014,U+00B7` for **IBM Plex Mono 400 + 600** and **Space Grotesk SemiBold** (all SIL OFL; skip Archivo — body copy is CJK-dominant, A12's stack covers Latin body). Inline as `@font-face` base64 in `HTML_TEMPLATE`'s first `<style>` before `:root` (static text — no `.replace`/`__DATA__` hazard; base64 alphabet can't break the `r'''…'''` template), with `unicode-range:U+0020-007E` so CJK keeps falling to PingFang per-character. The `zero` feature MUST survive subsetting (CSS at 1852 requests it). **Prerequisite decision (TYPO-6):** unify tickers on mono 600 *before* inlining — `.row .sym` (**2056**) and `.frow .fsym` (**2263**) → `var(--f-mono)` (`.smc-sym` 2224 already is); reserve Space Grotesk for the masthead h1 (1879), card titles (2091), and the B2 verdict. Verify with a canvas width-probe that all three stacks resolve to embedded faces. Effect: the entire declared identity becomes real on every surface at once — KPI heroes, tables, chart axes, tickers. Effort **M**. **+50–60KB** (the plan's only material spend).

**B2. THE SIGNATURE — "The Morning Front Page"** — *SPC-1 (P0) + DIST-3 + DIST-6 + MOT-2(A7); spatial, distinctiveness*
(1) **Typographic apex:** in `insightBanner()` (**3089**) set the verdict span to `font-family:var(--f-disp);font-size:clamp(17px,1.5vw,21px);line-height:1.35`; keep the severity tick/icon tone-colored, verdict text `var(--txt)`. Demote the card's 20px 今日要点 title to a `.cap`-style 11px uppercase label. (2) **Order:** swap the concat at **3308** to `insightBanner()+onboardStrip()` (verdict above meta-instruction) — keep the dismiss handler (**3857**) and insufficient-data path (**3307**) consistent. (3) **One emphasized tier, used exactly twice:** a `.verdict` class (~10 lines of CSS near 2180, `--panel2` ground) applied to the hero line and the QQQ 决策台 headline at **3144**. (4) **The signature mark, surfaced daily:** render a 6px-tall ~60-day regime mini-ribbon from `DATA.qqqTqqq.series.slice(-60)` colored by `qStateColor()` (per-day `state` already ships in the payload — Python emits it at 970/1125) as one `<svg>` of merged rects **inside the always-visible hero row (3089–3104)** — NOT inside the collapsed 看其他信号 details at 3105. Data-driven, no hard-coded tickers, zero payload growth. Effect: masthead → display-scale verdict with regime ribbon → four KPIs → ledger, all sharing one gutter (A9) and rising in priority order (A7) while the amber line draws in (A1). The thing the user remembers. Effort **M**. **+~1.5KB.**

**B3. Two-tier hierarchy pass** — *SPC-3 + SPC-2 + SPC-7 (P1/P2); spatial*
(1) **SPC-2:** stop appending the 期权交易 ledger outside the seg panels (**4116–4127**, hooked at 3320); render it only inside the `nw` seg next to 期权敞口, wrapped in the existing `foldCard()` (**3117**) default-closed with summary `期权交易 · 净现金流合计 <amount>`; keep only the short 计算口径说明 note as a universal tail. Kills the identical ~900px coda on all 11 tabs. (2) **SPC-3:** add `.card.ref{background:var(--bg2);border-color:var(--hair)}` + `.ref .dh .t{font-size:var(--t-md)}`; apply to 计算口径说明, 期权交易, honesty boxes, folded reference cards; tier-1 decision cards (决策一览, QQQ 决策台, 全账户净值) keep 20px titles + a 2px top rule in `--line`. (3) **SPC-7:** `@media (min-width:981px){.seg[data-seg="score"] .score-tablewrap{max-height:calc(100vh - 300px)}}` (**2215**/2970 — calc variant keeps the sticky thead working). Effect: the eye gets a ranking; the decision roster owns the column. Effort **M**. **~0 bytes.**

**B4. Enforce the type ramp + tokenize letter-spacing** — *TYPO-3 + TYPO-5 + TYPO-7-fractions (P1/P2); typography*
`body{font-size:var(--t-base)}` (**1851**); collapse 10/10.5px micro-labels → `--t-xs:11px` (`th` 2194, `.badge .l` 2124, `.kpi .l` 1925, `.tag` 2206, `.legend` 2134); add `--t-num:22px` / `--t-num-hero:26px`; retire 23px `.hero-fig` → `--t-num` (**2114**); **map** (do NOT delete — they're real overrides per verification) the 18 inline `font-size:13px` JS sites to `var(--t-base)`; 12/12.5px → `--t-sm` (keep 12.5 as the one fractional token), 13.5 → 13 (**2183**). Letter-spacing: `--ls-label:.07em` (all uppercase micro-labels), `--ls-tight:-.01em` (display titles + large numerals), keep `.11em` as the masthead-only exception; convert inline `.6px` at **3334/3336**. Target: every `font-size`/`letter-spacing` is a token. Effort **M**. **~0 bytes.**

**B5. Enforce amber chart grammar — QQQ weather chart only** — *COLOR-2 (P1, scoped per verification); color*
In the A4 `QTC` const: `ema8 '#E8B339' → '#B89030'` (propagates to line 2830, legend 3224, tooltip 2837); in the same pass re-map `qStateColor.ema21 '#B89030' → '#888D96'` (**2813**) and the EMA21 zone fill `'#E8B339' → '#B89030'` (**2826**) to avoid new collisions. The per-stock grey price line is **deliberately kept** (see Non-goals). Effect: the only pure amber on the QQQ panel is regime/overheat state — amber never decorates an indicator. Effort **S**. **0 bytes.**

**B6. Single JS palette const** — *COLOR-4 (P2); color*
`const C={accent:'#E8B339',ref:'#B89030',green:'#4FB286',red:'#E5707A',mut:'#888D96',mut2:'#B6BAC1',subj:'#D9DCE1',grey:'#6B7079',hair:'#1A1C21',bg:'#0B0C0E',panel:'#121316'}` near `CHARTREG` (**2735**); replace the ~150 hex literals in `chart()/svgLines()/qqqStrategyChart()/nwChart/fibChart/FIBCOL/momColor`, promoting the recurring off-token greys (#6B7079, #D9DCE1, #AEB4BE, #4B4F58, #5F6168, #9A8A4A, plus #7F8794/#555A63 in `wholeAccountCard`) to a named, documented grey ramp; also point the knockout strokes at **2833, 3755–3756** at `C.bg` (pre-work for C7's ladder change). **Caution:** several literals carry concatenated alpha suffixes (`${col}66`) — replace value-by-value, then `node --check`. Effort **M**. **~0 bytes (slightly negative).**

**B7. The `axisFrame()` chart kit: nice ticks, calendar dates, responsive geometry** — *DV-03 + DV-02 (P0/P1), dataviz bold move; dataviz*
Shared helpers near 2735: `niceTicks(min,max,n)` (1/2/5·10^k step rule, snap domain outward, include 0 when `zero:true`) and `monthTicks(xmin,xmax)` (month boundaries labeled M/1, year-marked `26/1` at January). Replace the five `i/4`/`i/5` loops (**2753–2755, 2794–2796, 2827, 2850–2851, 3741–3742**) and equal-fifths x-ticks (**2756–2758, 2798–2799, 2828, 2852–2853, 3743–3744**). Oscillator fixed domain `[-105,105] → [-100,100]` (**3798**); adaptive y-decimals in `chart()` (**2755**). **DV-02:** per-builder `const MOB=matchMedia('(max-width:560px)').matches`; `W=MOB?520:900`, margins ×0.75 — text renders ~8px instead of ~5px on a 390px phone; charts rebuild on every render so no resize listener. Effort **M**. **Net-negative bytes** (deletes repeated frame code).

**B8. De-congest trade markers; let the data layer win** — *DV-04 (P1); dataviz*
In the txns loop (**2775–2779**): pre-merge same-day same-side trades (summed |amount|, amount-weighted price); cap r at 10; BUY fill-opacity 0.55→0.3 with stroke-width 1.2; raise the price line to stroke-width 2 `#8A8F98` (via `C`). Adapt the `bindMarkers` tooltip (**3921–3931**) to list merged trades. Effect: NVDA's recent two months stop being a green blob. Effort **M**. **~0 bytes.**

**B9. Differentiate the benchmark twins + de-overlap end labels** — *DV-07 (P1); dataviz*
In `svgLines` (**2805**): honor per-def `width` (portfolio 2.3, benchmarks 1.5) and per-def dash (Nasdaq `'1.5 3'` dotted vs S&P `'5 3'`) set in the cmp defs (**3276**); after computing end labels (**2806–2807**), one-pass vertical relaxation pushing successive labels ≥12 user units apart. Backward-compatible with the oscillator/RSI callers. Effort **S**. **~0 bytes.**

**B10. Complete "The Morning Open": gauges sweep, one characterful hover** — *MOT-4 (P1); motion*
(a) `@keyframes barIn{from{width:0}}` + `body.ready .fbar .p{animation:barIn .5s var(--ease) backwards}` in the no-preference block — from-only keyframe animates to each bar's inline width, zero JS, replays on panel reveal (note: diverging drift bars grow from their outer edge — acceptable). (b) On `.card:hover`, grow the title tick (**2097–2100**) to ~1.4× `var(--tick-s)` and tint `--line → --mut` (grey, never amber); on `.row:hover`, ease `border-left-color` to `var(--line)` (2048/2050 already transition border). Add `.fbar .p{animation:none}` to the reduce block. Effort **M** (split a/b). **~0 bytes.**

### Phase C — Polish & atmosphere

**C1. Engraved chart plates (all FIVE builders) + nwChart de-banding** — *BG-2 + BG-3 (P1) + DV-10 (P2); atmosphere, dataviz*
Unify grids to `--hair` `#1A1C21` (fix `chart()`'s darker-than-panel vertical stroke `var(--bg2)` at **2757** — DV-10); add a hairline plot-frame `<rect>` to chart/svgLines/qqqStrategyChart/nwChart/fibChart (**2736, 2782, 2815, 2841, 3731** — qqqStrategyChart included or the plate is inconsistent); in `chart()`, emit one `<linearGradient id="g${cid}">` (#6B7079 .08→0, cid is collision-safe) + under-line polygon. **BG-3:** delete nwChart's per-segment momentum polygons (**2854–2855**), replace with ONE polygon under the whole curve filled by an amber-fade gradient `rgba(232,179,57,.07)→0`; the bottom strip (**2860–2861**) stays the sole momentum encoder (`col()` still serves the tooltip at 2864). Also swap the tooltip's static 现价 row (**2771**) for a per-day `距现价 ±x.x%` delta. Effort **M**. **+~300 bytes.**

**C2. Run-merge per-day tiling** — *DV-06 (P1); dataviz*
Group consecutive same-color days into single shapes in the fibChart ribbon (**3746–3748**) and strip (**3763–3764**), qqq strip (**2831**), and the nwChart strip (**2861**, post-C1). Kills corduroy seams; cuts hundreds of DOM nodes (render cost — file size unaffected, these are view-time strings). Effort **M**. **0 bytes.**

**C3. Signal recency fade + truthful legends** — *DV-08 + DV-09 (P2); dataviz*
Fade fib triangles/resonance rings by age: `opacity=0.25+0.65*t` where `t=(date−xmin)/(xmax−xmin)`; rings r 10→8, stroke 2.2→1.6 (**3754–3759**); `svgLines` marks (**2793**) stroke-opacity 0.25, render only last-90-day marks at 0.4. Legend modifier classes next to **2137**: `.legend i.ln{width:16px;height:0;border-top:2px solid currentColor}` `.legend i.lnd{border-top-style:dashed}` — apply to the price-chart (**3832–3833**; BUY/SELL swatches are already circle/ring — leave them), cmp (**3275**), fib (**3789–3795**) legends; in-chart, make 现价 a solid 1px `#E8B339` rule vs dashed `#B89030` cost-basis (**2766–2768**) so the two ambers carry the live-vs-reference semantics. Effort **S**. **~0 bytes.**

**C4. Ledger-ize the left rail** — *DIST-5 (P2); distinctiveness*
In `renderHoldingsList` (**~2705–2725** — NOT 3398; `renderList` at 2730 only delegates): add an uppercase micro-caption header row `持仓 · HOLDINGS · ${a.length}` styled like `.kpi .l` (1924–1930), and `border-left:1px solid var(--hair)` on `.row .pnl` (~2065). `.row .meta` is already mono (2062) and `.pnl` already right-aligned — no further edits. Effort **S**. **~0 bytes.**

**C5. Mobile composition diet** — *SPC-6 (P1, real-device cost ~500–600px); spatial*
At ≤560px: render the holdings nav behind a default-closed foldcard (`组合总览 · N 持仓 ▸`) when the active overview seg is `score` (pure duplication of the decision cards there); compress `onboardStrip` to one line (`从这里开始 →` + dismiss) via an `.onboard-compact` rule. Effort **M**. **~0 bytes.**

**C6. Red prose discipline on the QT tab** — *COLOR-7 (P2); color*
Hero verdict (**3218**) and Brief 现在 headline (**3144**, post-B2 `.verdict`): text → `var(--txt)` with a 3px red tick (reuse the **2097–2099** `::before` pattern, background `tc`) + only the leading verb-phrase span tone-colored; keep the colored border-left rail and state chip. One red line max per card. (The 不要做 line is amber at 3146 — already correct.) Effort **S**. **0 bytes.**

**C7. Surface & chrome finishes** — *COLOR-5 + DIST-4 + BG-5 + MOT-7 (P2); atmosphere, color, motion*
(a) Widen the surface ladder one perceptible notch at **1818**: `--bg → ≈#060608` (or raise `--panel`) and `--panel2 → #1B1E24` (targets bg/panel ≈1.09:1, panel/panel2 ≈1.11:1); re-verify AA on new panel2 + masthead color-mix (1872); knockout strokes already routed via B6. (b) Card inset top-light: `box-shadow:inset 0 1px 0 rgba(255,255,255,.02)` at **2087**, echoing the masthead bevel. (c) Scrollbars: tokenize `#363B45` (**1861**) → `color-mix(in srgb,var(--mut) 35%,var(--panel))`; Firefox fallback scoped `@supports (-moz-appearance:none){html{scrollbar-width:thin;scrollbar-color:var(--line) transparent}}` — NOT unscoped (Chromium 121+ would drop the custom webkit bars). (d) Floating chrome (**2173–2177**): `transition:opacity .18s var(--ease),translate .18s var(--ease),display .18s allow-discrete` + `@starting-style{opacity:0;translate:0 -4px}` (totop: `0 6px`), AND set the faded values on the `[hidden]` states (`opacity:0` + translate) so the exit eases too; `translate` composes with `.ctx`'s `transform:translateX(-50%)`; graceful no-op in older engines; fix the false comment at **2286**. Keep `.tt` a display toggle. Effort **S each**. **~0 bytes.**

---

## 5. Explicit non-goals

1. **No CJK webfont embedding, ever** — Noto Sans SC is multi-MB; PingFang per-character fallback IS the design.
2. **No recoloring the per-stock grey price line to amber** (the first half of the color bold move / COLOR-2) — it's a documented, self-consistent in-card convention (legend 3832–3833, 怎么读 note 3835), it would mark exited stocks "live," and it re-inflates amber against A3's direction. The QQQ EMA8 swap (B5) is the only genuine breach.
3. **No deleting the 18 inline `font-size:13px`** (TYPO-3's original step) — verification proved they actively override 15px/23px contexts; B4 tokenizes them instead.
4. **No unscoped `scrollbar-color` on `html`** (BG-5's original rec) — Chromium 121+ honors it and silently disables the custom webkit scrollbars.
5. **No plain-`calc()` bloom anchor** (SPC-8's original rec) — regresses mid-width viewports; A2 uses the `min()` clamp.
6. **No literal DOM fusion of the verdict into `#kpis` as a fifth ruled row** (the spatial bold move taken literally) — the dismiss/insufficient-data render paths (3857, 3307) and the strip build (2535–2537) make it a high-risk refactor for marginal gain over B2's visual fusion via shared gutter, scale, and choreography.
7. **No IA restructuring** — tab merges, default-seg changes, drill-down consolidation are UIUX_REVIEW territory and were deliberately closed there.
8. **No maximalism** — no animated backgrounds, no gradient-washed cards, no new hues, no third hero-numeral size, no decorative green/red, no glassmorphism. The skill's bar is met here by precision, scarcity, and one signature — not spectacle.
9. **No hard-coded tickers anywhere** — the B2 regime ribbon reads the data-driven `DATA.qqqTqqq.series` payload key; chart code stays symbol-agnostic.

---

## 6. Validation protocol (run after EACH phase)

1. **Regenerate + gate:** `python3 sync.py --no-fetch` → must print `got N/N tickers` and the verification block `OK` for marketValue / unrealized / numHeld.
2. **JS validity:** extract the `<script>` from `output/portfolio_dashboard.html` and run `node --check` — mandatory after every template edit (nested template literals; a stray backtick fails silently). Confirm the `__DATA__` injection point is untouched in `render_html`.
3. **Visual audit:** re-run `scripts/_audit_shots.py`; eyeball `ov_score` / `ov_nw` / `stk_price` desktop **and** mobile: payload parses (no console errors), held stock shows price line + buy/sell markers + avg-cost step, KPI strip and crosshair tooltips intact, reduced-motion mode shows no animation (toggle `prefers-reduced-motion` in DevTools).
4. **Payload budget:** `wc -c output/portfolio_dashboard.html` vs the **738,554 B** current baseline (711,297 B June-8 reference). Cumulative growth across all phases must stay ≤ ~120KB; expected actuals — Phase A ≈ +1KB, Phase B ≈ +55–65KB (fonts), Phase C ≈ +1KB.
5. **Phase-specific checks:** after **A1/A8** — flip tabs rapidly; draw-in and segIn replay without lag or double-animation. After **A3/B5** — re-screenshot mobile score tab; amber share visibly drops; legend/tooltip/stroke colors agree on the QQQ chart. After **B1** — canvas width-probe in headless Chrome confirms `--f-disp`/`--f-ui`/`--f-mono` resolve to the embedded faces with distinct widths, and slashed zeros render in KPI/table/axis numerals. After **B7** — y-axes show 1/2/5-stepped values including 0 where relevant; x-axes show month boundaries with a `26/1` year mark; 390px-wide screenshot shows legible chart text. After **B6/anything touching chart JS** — spot-check one `${col}66` alpha-concat site renders identically.
