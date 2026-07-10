"""Focused regression tests for the self-contained dashboard UI shell."""

from __future__ import annotations

import re

import generate


def test_render_html_makes_executable_data_script_safe() -> None:
    hostile = "</script><script>alert('broker text')</script>&\u2028\u2029"
    html = generate.render_html({"hostile": hostile})

    # The template owns the only real closing script tag. Payload text cannot
    # terminate it, start markup, or retain JS line-separator code points.
    assert html.count("</script>") == 1
    assert hostile not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in html
    assert "\\u0026" in html
    assert "\\u2028\\u2029" in html
    assert "__DATA__" not in html


def test_hostile_broker_and_artifact_text_stays_data_not_markup() -> None:
    payload = {
        "stocks": [{"sym": "BAD", "name": '<img src=x onerror="alert(1)">'}],
        "artifactHealth": {
            "artifacts": {
                "research": {
                    "status": "provisional",
                    "reason": "</script><svg onload=alert(2)>",
                    "decisionGrade": False,
                }
            }
        },
        "sources": [{"url": "javascript:alert(3)", "name": "bad source"}],
    }
    html = generate.render_html(payload)

    assert "<img src=x" not in html
    assert "<svg onload" not in html
    assert html.count("</script>") == 1


def test_dashboard_uses_one_alert_ledger_and_artifact_health_fallback() -> None:
    src = generate.HTML_TEMPLATE

    assert "function buildAlertModel()" in src
    assert "const AM=buildAlertModel()" in src
    assert "const ALERTS=buildAlertModel()" in src
    assert "function artifactHealthEntries()" in src
    assert "DATA.artifactHealth" in src
    assert "marginShortPutContracts" in src
    assert "account:margin-unknown" in src
    assert "research:aics-history" in src
    assert "'provisional'" in src
    assert "x.decisionGrade===false" in src
    assert "research:aics-decision-grade" in src
    assert "x.stale===true||x.decisionGrade===false" not in src
    assert "const researchArtifacts=" in src
    assert "个数据产物受限/待复核" in src


def test_dashboard_preserves_routes_and_escapes_search_echo() -> None:
    src = generate.HTML_TEMPLATE

    assert "function aicsJump(id)" in src
    assert 'href="#aics-' not in src
    assert "「${esc(q)}」" in src
    assert "「${q}」" not in src
    assert "role=\"listbox\"" not in src


def test_primary_external_text_and_urls_are_hardened() -> None:
    src = generate.HTML_TEMPLATE

    assert "const safeUrl=value=>" in src
    assert "u.protocol==='https:'||u.protocol==='http:'" in src
    assert 'href="${s.url}"' not in src
    assert "const u=safeUrl(s.url)" in src
    assert "rel=\"noopener noreferrer\"" in src
    assert '<h2 class="t">${esc(s.sym)}</h2><span class="nm">${esc(s.name)}</span>' in src
    assert 'data-aics-preset="${esc(s.id)}"' in src
    assert "aicsScenarioApplyPreset('${s.id}')" not in src


def test_dynamic_rows_do_not_interpolate_symbols_into_javascript() -> None:
    src = generate.HTML_TEMPLATE

    assert "onclick=\"stockGo('${" not in src
    assert "onclick=\"MMSEL='${" not in src
    assert 'onclick="stockGo(this.dataset.sym)' in src
    assert 'onclick="marketMassSelect(this.dataset.mmSym)' in src
    assert "if(!mmSymbols().includes(sym))return" in src


def test_local_storage_rendering_is_allowlisted_and_escaped() -> None:
    src = generate.HTML_TEMPLATE

    assert "['cap','equal','invvol'].includes(s.policy)" in src
    assert "['edge','center'].includes(s.glide)" in src
    assert "const jesc=s=>esc(s||'')" in src
    assert 'value="${esc(v==null?\'\':String(v))}"' in src


def test_decision_and_market_research_copy_is_html_escaped() -> None:
    src = generate.HTML_TEMPLATE

    assert "${esc(dp.headline||st.label)}" in src
    assert "${esc(c.verdict||'')}" in src
    assert "${mmEsc((item.role||[]).join(' · '))}" in src
    assert "${esc(D.asOf||'—')}" in src


def test_memory_flow_route_has_a_real_panel_not_only_a_nav_button() -> None:
    src = generate.HTML_TEMPLATE

    assert "memflow:()=>memoryFlowCard()" in src
    assert re.search(r"const segs=\[[^\]]*'memflow'", src)
    assert "data-seg=\"memflow\"" in src
    assert "P.sharesIssuedDenominatorVerified" in src
    assert "mfNum(P.adsPerLocalShare,0)" in src
    assert "10 ADS = 1 common" not in src


def test_mobile_views_do_not_silently_truncate_evidence() -> None:
    src = generate.HTML_TEMPLATE

    assert "researchRows.slice(0,14).map" not in src
    assert "return rows.slice(0,14).map" not in src
    assert "const mobile=rows.slice(0,16).map" not in src
    assert ".recent-actions" in src
    assert "class=\"stock-jump\"" in src
    assert ".seg[data-seg=\"tx\"] table th:nth-child" not in src


def test_account_copy_and_composition_do_not_overstate_cash() -> None:
    src = generate.HTML_TEMPLATE

    assert "已知正现金" in src
    assert "资产（正额分项）" in src
    assert "负债 / 净负额（单独展示）" in src
    assert "保证金借记 (margin debit) 与购买力不在本导出中" in src
    assert "历史验证不足" in src
    assert re.search(r"hist0\.status==='insufficient_history'", src)


def test_return_and_realized_quality_are_visible_and_alertable() -> None:
    src = generate.HTML_TEMPLATE

    assert "S.twrQuality" in src
    assert "S.mwrQuality" in src
    assert "S.realizedPnlQuality" in src
    assert "quality:twr" in src
    assert "quality:mwr" in src
    assert "quality:realized" in src
    assert "s.realizedConfidence" in src
    assert "s.realizedBasisScope" in src
    assert "s.realizedWarnings" in src


def test_partial_risk_slice_and_mobile_controls_are_explicit() -> None:
    src = generate.HTML_TEMPLATE

    assert "风险贡献覆盖 ${cq.coverageWeightPct??0}%" in src
    assert "覆盖切片资金" in src
    assert "低于 ${cq.minimumCoverageWeightPct??80}% 会整块停用" in src
    assert '[role="button"].note-lk' in src
    assert "min-width:var(--tap-min);min-height:var(--tap-min)" in src
    assert ".ctx{right:8px; left:auto; transform:none; max-width:70vw; font-size:var(--t-sm); padding:9px 14px; cursor:pointer; min-height:var(--tap-min)}" in src
    assert "#qbar::after" in src


def test_ai_watch_entry_proxies_are_not_mislabeled_as_valuation_or_earnings() -> None:
    src = generate.HTML_TEMPLATE

    assert "d.pricePositionBand||'—'" in src
    assert "d.priceVolatilityRisk||'—'" in src
    assert "Price vs trailing median" in src
    assert "Price-volatility proxy" in src
    assert "d.valuationBand||'—'" not in src
    assert "d.postEarningsGapRisk||'—'" not in src


def test_market_freshness_uses_local_dates_and_standard_exchange_holidays() -> None:
    src = generate.HTML_TEMPLATE

    # Date-only strings must not be parsed as UTC and shifted to the prior day
    # in US time zones. Standard exchange holidays are not trading sessions.
    assert "new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),12)" in src
    assert "function usMarketHolidays(y)" in src
    assert "!usMarketHolidays(d.getFullYear()).has(dateKey(d))" in src


def test_close_vs_intraday_ui_respects_producer_quality_gate() -> None:
    src = generate.HTML_TEMPLATE

    assert "m.dataQualityStatus==='PASS'" in src
    assert "DATA INSUFFICIENT" in src
    assert "allPortfolioWindowsPass" in src
    assert "ok:qualityOk(w)" in src
