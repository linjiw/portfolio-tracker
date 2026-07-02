export const meta = {
  name: 'dashboard-full-audit',
  description: 'Walk every panel/tab of the portfolio dashboard: design consistency + functionality + best-in-class gaps, adversarially verified, synthesized into one prioritized plan',
  phases: [
    { title: 'Audit', detail: 'one auditor per panel — functionality, consistency, clarity, brief-gaps' },
    { title: 'Verify', detail: 'adversarial re-read of each finding against the real code' },
    { title: 'Synthesize', detail: 'dedup + prioritize + scorecard + gap map + north-star' },
  ],
}

const REPO = process.env.PORTFOLIO_TRACKER_ROOT || process.cwd()
const GEN = REPO + '/generate.py'
const SHOTS = REPO + '/output/audit_shots'
const REF = SHOTS + '/_DESIGN_REF.md'
const BRIEF = SHOTS + '/_BRIEF.md'

const FINDING = {
  type: 'object', additionalProperties: false,
  required: ['id', 'type', 'severity', 'title', 'where', 'evidence', 'proposed_fix', 'confidence'],
  properties: {
    id: { type: 'string' },
    type: { type: 'string', enum: ['functional', 'consistency', 'clarity', 'accessibility', 'mobile', 'copy', 'brief_gap', 'polish'] },
    severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] },
    title: { type: 'string' },
    where: { type: 'string' },
    evidence: { type: 'string' },
    proposed_fix: { type: 'string' },
    confidence: { type: 'number' },
  },
}
const AUDIT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['part', 'pillar', 'design_score', 'functional_ok', 'strengths', 'findings'],
  properties: {
    part: { type: 'string' },
    pillar: { type: 'string' },
    design_score: { type: 'number' },
    functional_ok: { type: 'boolean' },
    strengths: { type: 'array', items: { type: 'string' } },
    findings: { type: 'array', items: FINDING },
  },
}
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['part', 'design_score', 'confirmed', 'rejected', 'missed'],
  properties: {
    part: { type: 'string' },
    design_score: { type: 'number' },
    confirmed: { type: 'array', items: FINDING },
    rejected: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['title', 'why'], properties: { title: { type: 'string' }, why: { type: 'string' } } } },
    missed: { type: 'array', items: FINDING },
  },
}
const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['scorecard', 'consistency_themes', 'prioritized_fixes', 'brief_gap_map', 'north_star'],
  properties: {
    scorecard: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['part', 'design_score', 'functional_ok', 'one_line'], properties: { part: { type: 'string' }, design_score: { type: 'number' }, functional_ok: { type: 'boolean' }, one_line: { type: 'string' } } } },
    consistency_themes: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['theme', 'severity', 'instances', 'fix_direction'], properties: { theme: { type: 'string' }, severity: { type: 'string' }, instances: { type: 'string' }, fix_direction: { type: 'string' } } } },
    prioritized_fixes: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['rank', 'severity', 'type', 'part', 'title', 'where', 'fix', 'effort'], properties: { rank: { type: 'number' }, severity: { type: 'string' }, type: { type: 'string' }, part: { type: 'string' }, title: { type: 'string' }, where: { type: 'string' }, fix: { type: 'string' }, effort: { type: 'string' } } } },
    brief_gap_map: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['capability', 'status', 'home_panel', 'value', 'effort'], properties: { capability: { type: 'string' }, status: { type: 'string' }, home_panel: { type: 'string' }, value: { type: 'string' }, effort: { type: 'string' } } } },
    north_star: { type: 'array', items: { type: 'string' } },
  },
}

// ---- the audit units: every panel + cross-cutting concern ----
const S = n => SHOTS + '/' + n
const PARTS = [
  { key: 'chrome_kpi', title: '顶部框架 · KPI 账本条 (masthead + KPI ledger strip + 数据窗口)', pillar: 'TRUTH', reads: 'generate.py 2155-2215 (header/body chrome + kpis[] array + range label)', shots: [S('ov_score_desktop.png'), S('ov_score_mobile.png')], focus: 'Are the 11 KPIs the RIGHT first-glance truths? Do they separate market gain from deposits, realized vs unrealized, options, cash %? Is cash %/balance shown? Is the strip readable & ordered by importance? Mobile legibility of the dense strip.' },
  { key: 'insight', title: '今日要点 / 顶部洞察横幅 (insight banner + bridge card)', pillar: 'TRUTH', reads: 'generate.py 2531-2587 (bridgeCard + insightBanner)', shots: [S('ov_score_desktop.png'), S('ov_score_mobile.png')], focus: 'Is this a calm, decision-focused "top alerts / what changed since last review" surface, or noise? Are links accurate? Does it avoid dopamine framing?' },
  { key: 'list_rail', title: '左侧持仓列表 (search + sort + held/exited/all + rows)', pillar: 'TRUTH', reads: 'generate.py 2155-2180 (controls) + 2216-2241 (filtered, renderList)', shots: [S('ov_score_desktop.png'), S('ov_score_mobile.png')], focus: 'Holdings table quality: sort options, fib dot semantics, row info hierarchy, empty/exited states, keyboard. Mobile stacking.' },
  { key: 'score', title: '决策一览 (scorecard — 技术/风险/行为/再平衡 同屏)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2428-2468 (scorecardCard)', shots: [S('ov_score_desktop.png'), S('ov_score_mobile.png')], focus: 'Is the per-holding joint signal table honest & non-advice? column overload on mobile? sample-size handling?' },
  { key: 'qt', title: 'QQQ/TQQQ 决策台 (decision brief, ownership/cash constraint, CCS, spreads, legs, playbook, weather chart)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2588-2698 (qqqTqqqTab) + 2315-2342 (qStateColor/Label, qqqStrategyChart)', shots: [S('ov_qt_desktop.png'), S('ov_qt_mobile.png')], focus: 'Heavy bespoke tab — is it consistent with card grammar? Does it stay non-advice while being directive ("执行地图")? options first-class quality. Is this special-case content out of place vs the rest?' },
  { key: 'nw', title: '净值 · 全账户 (whole-account + options exposure + spread ledger + diagnostics)', pillar: 'TRUTH', reads: 'generate.py 2469-2530 (wholeAccountCard, optionsExposureCard, optionsSpreadLedgerCard, diagnosticsCard) + 2343-2369 (nwChart) + 3308-3326 (renderOptions)', shots: [S('ov_nw_desktop.png'), S('ov_nw_mobile.png')], focus: 'The TRUTH home: cash, buying power, options gross/net, leverage visibility. Premium-vs-underlying P&L? Greeks/DTE/assignment risk? Does it reconcile?' },
  { key: 'risk', title: '风险 (drawdown curve + rolling vol + risk contribution)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2847-2887 (riskCard + rebalVolMap)', shots: [S('ov_risk_desktop.png'), S('ov_risk_mobile.png')], focus: 'Risk-before-return. Present: maxDD?, vol, risk-contribution. MISSING vs brief: Sharpe/Sortino, beta, downside dev, scenario stress, correlation? Is this the home for them? Honest small-sample framing.' },
  { key: 'struct', title: '结构 (asset-class + theme/sector + concentration HHI + 盈亏贡献)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2376-2427 (structureCard + contributionCard)', shots: [S('ov_struct_desktop.png'), S('ov_struct_mobile.png')], focus: 'Allocation X-ray quality. Look-through into ETFs? target vs current? concentration warnings. Contribution analysis honesty.' },
  { key: 'cmp_overview', title: '指数对比 + 组合主曲线 (renderOverview value curve + fib overlay + TWR-vs-benchmark)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2699-2745 (renderOverview) + 2242-2314 (chart, svgLines)', shots: [S('ov_cmp_desktop.png'), S('ov_cmp_mobile.png')], focus: 'TWR vs SPY/QQQ clarity. MISSING: money-weighted IRR/MWR & the TWR-vs-MWR gap (timing skill) — is this the home? Are deposits clearly separated from gains in the curve?' },
  { key: 'pfib', title: '组合斐波那契 · 技术 (portfolio EMA ribbon / momentum)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2772 + 2784-2813 (portfolioFibCard, fibBadges, postureOf)', shots: [S('ov_pfib_desktop.png'), S('ov_pfib_mobile.png')], focus: 'MUST stay framed as technical reference, NOT advice. Consistency of the bespoke chart. Does a portfolio-level TA panel earn its place in a calm long-term cockpit?' },
  { key: 'sig', title: '持仓信号 (positionSignals + 今日共振 + 斐波那契动能排行)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2746-2832 (resonanceCard, fibRanking, momColor, positionSignalsCard)', shots: [S('ov_sig_desktop.png'), S('ov_sig_mobile.png')], focus: 'Non-advice framing, overtrading risk (does it nudge action?), table density on mobile, glossary on TA terms.' },
  { key: 'beh', title: '行为决策 (behavior flags — Thaler-based)', pillar: 'MATURITY & BEHAVIOR', reads: 'generate.py 2833-2846 (behaviorCard)', shots: [S('ov_beh_desktop.png'), S('ov_beh_mobile.png')], focus: 'Does it make behavior visible & a little uncomfortable (honest)? Tilt/streak detection? Or is it thin vs Edgewonk/TradeZella bar?' },
  { key: 'journal', title: '交易日志 (weeklyReview + maturityCard + killerStat + emotionOutcome + unjournaled worklist)', pillar: 'MATURITY & BEHAVIOR', reads: 'generate.py 2971-3068 (journalLoad…journalCard, wireJournalTab) + 2992-2993 (maturityScore, maturityBand)', shots: [S('ov_journal_desktop.png'), S('ov_journal_mobile.png')], focus: 'THE DIFFERENTIATOR. Plan-adherence win-rate? mistake taxonomy with $ cost? MAE/MFE? emotion→outcome? maturity score on process not profit? thesis checkpoints? localStorage persistence robustness. This is where best-in-class matters most.' },
  { key: 'rebal', title: '再平衡计划 (planner + drift map + action list + honesty)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 2881-2970 (rebalDefault…rebalOutput, wireRebal)', shots: [S('ov_rebal_desktop.png'), S('ov_rebal_mobile.png')], focus: 'Rule-based (user-defined band) not advice — verify framing. Drift threshold, action list correctness, "do nothing in-band = discipline" message. localStorage rules.' },
  { key: 'detail_price', title: '个股详情 · 价格·操作 (price line + buy/sell markers + avg-cost step)', pillar: 'TRUTH', reads: 'generate.py 3176-3217 (renderDetail + segWire) + 3270-3307 (bindMarkers, bindCharts) + 2242-2283 (chart)', shots: [S('stk_price_desktop.png'), S('stk_price_mobile.png')], focus: 'Marker tooltip UX, avg-cost step clarity, legend, mobile touch. Consistency of the per-stock chart with overview chart.' },
  { key: 'detail_tx', title: '个股详情 · 交易明细 (transaction table + options)', pillar: 'TRUTH', reads: 'generate.py 3176-3208 (tx seg in renderDetail) + 3308-3326 (renderOptions)', shots: [S('stk_tx_desktop.png'), S('stk_tx_mobile.png')], focus: 'Ledger clarity, 含旧仓 legacy-lot labeling, realized vs estimated, fees, mobile table.' },
  { key: 'detail_fib', title: '个股详情 · 斐波那契 (per-stock EMA ribbon + momentum + RSI)', pillar: 'PERFORMANCE & RISK', reads: 'generate.py 3099-3175 (fibChart, renderFib)', shots: [S('stk_fib_desktop.png'), S('stk_fib_mobile.png')], focus: 'Non-advice TA framing, glossary, sample-size empty state, consistency with portfolioFibCard.' },
  { key: 'detail_journal', title: '个股详情 · 日志 (positionJournalEditor — entry capture)', pillar: 'MATURITY & BEHAVIOR', reads: 'generate.py 3069-3098 (positionJournalEditor, wireJournalEditor)', shots: [S('stk_journal_desktop.png'), S('stk_journal_mobile.png')], focus: 'Entry-at-decision capture: thesis, conviction, plan entry/exit/stop, size %, horizon, emotion tag — under a minute? Falsifiable thesis checkpoints? Is the form complete vs brief? Persistence.' },
  // ---- cross-cutting ----
  { key: 'x_designsystem', title: 'CROSS-CUTTING · 设计系统一致性 (tokens, color semantics, type scale, spacing, card grammar)', pillar: 'ALL', reads: 'generate.py 1612-2155 (entire CSS) — focus on token usage, .card/.dh/.t/.nm, .kpi, .seg-rail, .note, color vars', shots: [S('ov_score_desktop.png'), S('ov_nw_desktop.png'), S('ov_risk_desktop.png'), S('ov_struct_desktop.png'), S('ov_journal_desktop.png'), S('ov_qt_desktop.png'), S('ov_rebal_desktop.png'), S('stk_price_desktop.png')], focus: 'GLOBAL consistency. Find ad-hoc inline colors that duplicate/violate tokens (grep generate.py for #E8B339, #4FB286, #E5707A, style="). Decorative green/red. Inconsistent card headers. Spacing/radii drift. Amber over-use. This is the single most important consistency lens.' },
  { key: 'x_a11y', title: 'CROSS-CUTTING · 可访问性 & 键盘 (roles, aria, focus, keyboard, contrast)', pillar: 'ALL', reads: 'generate.py 1930-1940 (focus styles) + 3209-3219 (segWire, restoreSeg, ovGo) + 3251-3269 (gl tooltip) + 3320-3343 (keydown, updateCtx)', shots: [S('ov_score_desktop.png')], focus: 'tablist/tab/tabpanel roles, aria-selected, focus-visible, arrow-key tab nav, Esc, Enter/Space on rows, tooltip keyboard access, color-contrast of --mut/--faint on panels.' },
  { key: 'x_mobile', title: 'CROSS-CUTTING · 移动端 & 响应式', pillar: 'ALL', reads: 'generate.py 2090-2154 (@media blocks)', shots: [S('ov_score_mobile.png'), S('ov_qt_mobile.png'), S('ov_nw_mobile.png'), S('ov_risk_mobile.png'), S('ov_struct_mobile.png'), S('ov_journal_mobile.png'), S('ov_rebal_mobile.png'), S('stk_price_mobile.png'), S('stk_tx_mobile.png')], focus: 'horizontal overflow, table truncation, tap-target size, seg-rail scroll-snap, KPI strip wrap, chart legibility at 390px. Mobile-first quality.' },
  { key: 'x_copy', title: 'CROSS-CUTTING · 文案/语气/i18n/免责声明 一致性', pillar: 'ALL', reads: 'generate.py 2370-3060 — scan card titles (.t), subtitles (.nm), notes; check 非投资建议 disclaimer coverage, 怎么读 explainer coverage, gl() glossary coverage, Chinese consistency', shots: [S('ov_qt_desktop.png'), S('ov_struct_desktop.png'), S('ov_journal_desktop.png')], focus: 'Which signal/decision/technical panels LACK 非投资建议? Which advanced metrics lack a 怎么读 or gl() tooltip? Tone drift (any casino/dopamine wording)? English/Chinese mixing inconsistency? Plain-language quality.' },
  { key: 'x_functional', title: 'CROSS-CUTTING · 功能完整性 & 跨标签链接 & 状态', pillar: 'ALL', reads: 'generate.py 3176-3343 (renderDetail dispatch, segWire, restoreSeg, wiring, keydown, mutation obs) + grep all `data-seg=` cross-tab links and localStorage keys (ptrak.*)', shots: [], focus: 'Do all cross-tab onclick targets (data-seg="...") exist in BOTH overview & detail rails? localStorage key consistency (ptrak.seg.ov/stk, journal.v1, review.v1, onboard.v1, rebal). Dead code, double-render, MutationObserver leaks, restoreSeg edge cases. The render_html .replace gotcha. Any place a number could render NaN/undefined/—.' },
  { key: 'x_empty', title: 'CROSS-CUTTING · 空状态 / 新手引导 / 数据质量', pillar: 'ALL', reads: 'generate.py 3220-3250 (onboardStrip) + 2517-2564 (diagnosticsCard, bridgeCard) + scan render funcs for empty-state notes (需 ≥N 交易日 / 数据不足)', shots: [S('ov_nw_desktop.png')], focus: 'Are empty states clear & consistent (a stock with <21 bars, no journal entries, no options)? data-freshness/quality warnings present & honest? onboarding dismissable & restorable? Consistent empty-state copy pattern?' },
]

function auditPrompt(p) {
  return `You are a world-class product designer + quant-finance dashboard architect + behavioral-finance coach auditing ONE part of a personal portfolio "investor maturity cockpit" dashboard. Be rigorous, concrete, and honest — prefer FEW high-signal findings over a long noisy list.

PART: ${p.title}
PILLAR IT SERVES: ${p.pillar}
SPECIAL FOCUS: ${p.focus}

STEP 1 — Read the standards you judge against:
- Read ${REF} (the design system — judge CONSISTENCY against this exactly)
- Read ${BRIEF} (the north-star — judge BEST-IN-CLASS gaps against this)

STEP 2 — Read the implementation (file ${GEN}; use Read with offset/limit):
- ${p.reads}

STEP 3 — Look at the REAL rendered output (Read each image):
${p.shots.length ? p.shots.map(s => '- Read ' + s).join('\n') : '- (code-only audit; no screenshot needed)'}

STEP 4 — Audit across ALL lenses, report findings:
- FUNCTIONAL: does it actually work? broken/undefined refs, wrong or NaN/— numbers, dead onclick targets, logic bugs. (node --check already passes — hunt LOGIC, not syntax.)
- CONSISTENCY vs _DESIGN_REF: card grammar (.card/.dh/.t/.nm), amber rationed to ONE accent, green/red on numbers only (no decorative use), mono tabular numerals, note/怎么读 explainer present, 非投资建议 where needed, gl() glossary on advanced terms, token radii/spacing, NO ad-hoc inline styles re-inventing a token.
- CLARITY: every advanced metric explained in plain language w/ tooltip? progressive disclosure? honest framing (no vanity, no advice)?
- ACCESSIBILITY: roles/aria/focus/keyboard/contrast for THIS part.
- MOBILE: does the mobile screenshot hold up (overflow, truncation, tap targets)?
- COPY/TONE/i18n: Chinese consistency, plain language, calm (no casino/dopamine), disclaimer coverage.
- BRIEF GAP vs _BRIEF: is this the natural home for a missing best-in-class capability (MWR/IRR, Sharpe/Sortino, beta, MAE/MFE, look-through, plan-adherence win-rate, thesis checkpoints, dividend calendar, scenario stress, correlation)? Mark present / partial / missing.

HARD RULES (a proposed fix that breaks these is WRONG):
- Data is injected via .replace("__DATA__", json) — NEVER introduce .format()/f-strings.
- UI is nested JS template literals; any edit MUST keep \`node --check\` valid (watch backticks/\${}).
- green/red = P&L sign ONLY; amber stays rationed; numerals stay tabular+slashed-zero.
- NO buy/sell advice or price predictions (explicit non-goal). Keep TA panels framed as reference.
- Tickers/themes are runtime-derived — never hard-code them.

Output the schema: part name, pillar, design_score 0-10 (consistency vs _DESIGN_REF), functional_ok bool, 1-4 strengths, and findings[]. Severity: P0 = broken / wrong number / misleading; P1 = real consistency or clarity gap; P2 = worthwhile clarity/polish; P3 = cosmetic. Each finding needs a concrete proposed_fix with a file:line in \`where\`. Zero findings for an already-excellent lens is a valid, honest answer.`
}

function verifyPrompt(part, audit) {
  return `You are an ADVERSARIAL verifier. A prior auditor reviewed the dashboard part "${part}". Re-read the ACTUAL code and KILL anything wrong, exaggerated, or already-handled. Default to skepticism — a plausible-but-unverified finding must be rejected.

Read ${REF} briefly, then for EACH finding open ${GEN} at its \`where\` line(s) and check:
1. Is the evidence LITERALLY true in the code right now?
2. Is the proposed_fix correct AND does it respect the hard rules? (no .format/f-string; keeps \`node --check\` valid; green/red=P&L only; amber rationed; no hard-coded tickers; NO buy/sell advice; TA stays reference.)

FINDINGS TO VERIFY (JSON):
${JSON.stringify(audit.findings || [], null, 1)}

Auditor's design_score was ${audit.design_score}. Re-judge it yourself.
- confirmed[]: evidence true + fix sound (you may TIGHTEN the fix text).
- rejected[]: evidence false / overstated / already-handled — say why in \`why\`.
- missed[]: only CLEAR, high-confidence issues the auditor missed that you saw while reading (same finding shape).
Return the VERIFY schema with your own design_score.`
}

// ---- run ----
phase('Audit')
log(`Auditing ${PARTS.length} parts (panels + cross-cutting) → adversarial verify → synthesis`)

const verified = await pipeline(
  PARTS,
  p => agent(auditPrompt(p), { label: 'audit:' + p.key, phase: 'Audit', schema: AUDIT_SCHEMA }),
  (audit, p) => agent(verifyPrompt(p.title, audit), { label: 'verify:' + p.key, phase: 'Verify', schema: VERIFY_SCHEMA })
    .then(v => ({ key: p.key, part: p.title, pillar: p.pillar, design_score: v.design_score, confirmed: v.confirmed, rejected: v.rejected, missed: v.missed }))
)

const clean = verified.filter(Boolean)
const totalConfirmed = clean.reduce((a, v) => a + (v.confirmed || []).length + (v.missed || []).length, 0)
log(`Verified: ${clean.length}/${PARTS.length} parts, ${totalConfirmed} confirmed findings → synthesizing`)

phase('Synthesize')
// compact payload for synthesis
const payload = clean.map(v => ({
  part: v.part, key: v.key, pillar: v.pillar, design_score: v.design_score,
  findings: [].concat(v.confirmed || [], v.missed || []).map(f => ({
    type: f.type, severity: f.severity, title: f.title, where: f.where, fix: f.proposed_fix, confidence: f.confidence,
  })),
}))

const synthPrompt = `You are the lead design + engineering reviewer. Synthesize a full-dashboard audit into ONE decisive, deduplicated, actionable plan. This plan will be implemented directly, so be concrete.

Read ${REF} and ${BRIEF} first.

VERIFIED FINDINGS PER PART (JSON):
${JSON.stringify(payload, null, 1)}

Produce the schema:
1. scorecard: one row per part {part, design_score, functional_ok(infer: false if any P0 functional finding), one_line}. Cover ALL parts listed.
2. consistency_themes: cross-part PATTERNS (e.g. "N technical panels missing 非投资建议", "ad-hoc inline #E8B339 reused instead of var(--accent)", "no MWR/IRR anywhere"). Each {theme, severity, instances, fix_direction}.
3. prioritized_fixes: ONE deduplicated ranked list across the whole dashboard. Merge duplicates that appear in multiple parts into a single entry (note all locations in \`where\`). Order by severity (P0→P3) then leverage. Each {rank, severity, type, part, title, where, fix, effort(S/M/L)}. Aim for the ~25-40 highest-value items; don't pad.
4. brief_gap_map: best-in-class capabilities from _BRIEF {capability, status(present|partial|missing), home_panel, value(high|med|low), effort(S/M/L)}. Cover MWR/IRR, Sharpe/Sortino, beta, MAE/MFE, plan-adherence win-rate, mistake-cost $, thesis checkpoints, look-through, dividend calendar/income page, scenario stress, correlation, tax/NRA view.
5. north_star: 3-7 highest-leverage moves toward best-in-class that RESPECT the non-goals (no advice/prediction) and the calm/honest ethos.

Be decisive. Where two findings conflict, pick the better one. \`where\` and \`fix\` must be implementable.`

const plan = await agent(synthPrompt, { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA })

return { parts_audited: clean.length, total_findings: totalConfirmed, per_part: clean, plan }
