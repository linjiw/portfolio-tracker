#!/usr/bin/env python3
"""Focused AICS dashboard audit.

This is intentionally narrower than the full dashboard screenshot suite:
activate the AICS tab, verify the route/rendering contract in desktop and a
true 390px mobile iframe, and save screenshots for visual review.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
AICS_JSON = ROOT / "output" / "aics.json"
OUT_DIR = ROOT / "output" / "audit_shots" / "aics"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if "/" in candidate and Path(candidate).exists():
            return candidate
        if "/" not in candidate:
            try:
                out = subprocess.check_output(["which", candidate], text=True).strip()
            except subprocess.CalledProcessError:
                continue
            if out:
                return out
    raise SystemExit("Chrome/Chromium not found; cannot run AICS screenshot audit.")


def read_text(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def inject(html_text: str, script: str) -> str:
    marker = "</body></html>"
    if marker not in html_text:
        raise SystemExit("unexpected dashboard HTML tail")
    return html_text.replace(marker, f"<script>{script}</script>{marker}")


def audit_script(replace_body: bool) -> str:
    finish = (
        "document.body.innerHTML='';"
        "var pre=document.createElement('pre');"
        "pre.id='aics-audit-json';"
        "pre.textContent=JSON.stringify(result);"
        "document.body.appendChild(pre);"
    ) if replace_body else "window.__aicsAudit=result;"
    return f"""
(function(){{
 function visible(el){{if(!el)return false;var r=el.getBoundingClientRect();return r.width>0&&r.height>0;}}
 function activate(){{
  try{{if(typeof ovGo==='function'){{ovGo('aics',{{replace:true,scroll:false}});return;}}}}catch(e){{}}
  var b=document.querySelector('#right .seg-rail [data-seg="aics"]');
  if(b)b.click();
 }}
 function collect(){{
	  var panel=document.querySelector('#right .seg[data-seg="aics"]');
	  var map=document.getElementById('aics-map');
	  var wrap=panel&&panel.querySelector('#aics-scoreboard .score-tablewrap');
	  var sort=panel&&panel.querySelector('#aics-score-sort');
	  var cards=panel?Array.from(panel.querySelectorAll('.card,.foldcard')).filter(function(el){{var r=el.getBoundingClientRect();return r.bottom>0&&r.top<window.innerHeight;}}):[];
	  var focusables=panel?Array.from(panel.querySelectorAll('button,select,a[href],[tabindex]')).filter(visible):[];
  var result={{
   viewportWidth: window.innerWidth,
   viewportHeight: window.innerHeight,
   activeSeg: (document.querySelector('#right .seg-rail button.on')||{{}}).dataset ? document.querySelector('#right .seg-rail button.on').dataset.seg : null,
   panelTextLength: panel ? (panel.innerText||'').length : 0,
   firstViewportCards: cards.length,
   bodyScrollWidth: document.documentElement.scrollWidth,
   bodyClientWidth: document.documentElement.clientWidth,
   horizontalOverflowPx: document.documentElement.scrollWidth-document.documentElement.clientWidth,
   scoreboardRows: panel ? panel.querySelectorAll('#aics-scoreboard tbody tr').length : 0,
   scoreCards: panel ? panel.querySelectorAll('#aics-scoreboard .score-cards .smc').length : 0,
	   scoreboardWrapWidth: wrap ? Math.round(wrap.getBoundingClientRect().width) : 0,
	   scoreboardWrapClientWidth: wrap ? wrap.clientWidth : 0,
	   scoreboardScrollWidth: wrap ? wrap.scrollWidth : 0,
	   scoreSortOptions: sort ? Array.from(sort.options).map(function(o){{return o.value;}}) : [],
	   mapExists: !!map,
   mapCircles: map ? map.querySelectorAll('circle').length : 0,
   mapPaths: map ? map.querySelectorAll('path').length : 0,
   mapBoxWidth: map ? Math.round(map.getBoundingClientRect().width) : 0,
   mapBoxHeight: map ? Math.round(map.getBoundingClientRect().height) : 0,
   scenarioControls: panel ? panel.querySelectorAll('[data-aics-scen]').length : 0,
   attributionExists: !!(panel&&panel.querySelector('#aics-attribution')),
   alertRows: panel ? panel.querySelectorAll('#aics-alerts tbody tr').length : 0,
   backtestExists: !!(panel&&panel.querySelector('#aics-backtest')),
   focusableControls: focusables.length,
   allThScoped: Array.from((panel||document).querySelectorAll('thead th')).every(function(th){{return th.getAttribute('scope')==='col';}})
  }};
  {finish}
 }}
 setTimeout(function(){{activate();setTimeout(collect,1400);}},250);
}})();
"""


def chrome_common(chrome: str) -> List[str]:
    return [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--allow-file-access-from-files",
        "--force-device-scale-factor=1",
        "--virtual-time-budget=8000",
    ]


def run_dump(chrome: str, url: str, width: int, height: int) -> str:
    cmd = chrome_common(chrome) + [f"--window-size={width},{height}", "--dump-dom", url]
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=90)


def run_screenshot(chrome: str, url: str, width: int, height: int, out: Path) -> None:
    cmd = chrome_common(chrome) + [f"--window-size={width},{height}", f"--screenshot={out}", url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=True)


def extract_audit_json(dom: str) -> dict:
    match = re.search(r'<pre id="aics-audit-json">([\s\S]*?)</pre>', dom)
    if not match:
        raise AssertionError("audit JSON marker not found in dumped DOM")
    return json.loads(html.unescape(match.group(1)))


def write_temp_target(source_html: str, name: str, replace_body: bool) -> Path:
    path = OUT_DIR / f"_tmp_{name}.html"
    path.write_text(inject(source_html, audit_script(replace_body)), encoding="utf-8")
    return path


def mobile_host(target: Path, name: str, replace_body: bool) -> Path:
    path = OUT_DIR / f"_host_{name}.html"
    if replace_body:
        script = """
function pullAudit(){
  var f=document.getElementById('frame');
  var pre=f&&f.contentDocument&&f.contentDocument.getElementById('aics-audit-json');
  if(!pre){setTimeout(pullAudit,500);return;}
  document.body.innerHTML='';
  var out=document.createElement('pre');
  out.id='aics-audit-json';
  out.textContent=pre.textContent;
  document.body.appendChild(out);
}
setTimeout(pullAudit,500);
"""
    else:
        script = ""
    path.write_text(
        "<!doctype html><html><body style='margin:0;background:#000'>"
        f"<iframe id='frame' src='file://{target}' style='width:390px;height:844px;border:0;display:block'></iframe>"
        f"<script>{script}</script></body></html>",
        encoding="utf-8",
    )
    return path


def png_dimensions(path: Path) -> Tuple[int, int]:
    try:
        out = subprocess.check_output(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], text=True)
    except Exception:
        return 0, 0
    width = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
    height = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
    return width, height


def static_checks(dashboard_html: str, aics: dict) -> Dict[str, bool]:
    return {
        "navButton": 'data-seg="aics"' in dashboard_html and "AICS产业链" in dashboard_html,
        "routeAllowlist": "['score','decide','aisemi','aics','aiwatch','qt'" in dashboard_html,
        "loader": '"aics":' in dashboard_html or "'aics':" in dashboard_html,
        "emptyState": "暂无 AICS 数据" in dashboard_html and "python3 scripts/aics_tool.py" in dashboard_html,
        "disclaimer": "非投资建议" in dashboard_html and "Research framework only" in (aics.get("disclaimer") or ""),
        "dataShape": len(aics.get("scores") or []) >= 15 and len(aics.get("relationshipEdges") or []) >= 10,
    }


def assert_metrics(label: str, metrics: dict) -> List[str]:
    failures = []
    checks = {
        "active AICS tab": metrics.get("activeSeg") == "aics",
        "non-empty first viewport": metrics.get("panelTextLength", 0) > 1500 and metrics.get("firstViewportCards", 0) >= 1,
        "no page horizontal overflow": metrics.get("horizontalOverflowPx", 999) <= 4,
        "scoreboard rows": metrics.get("scoreboardRows", 0) >= 15,
	        "scoreboard contained": (
	            0 < metrics.get("scoreboardWrapWidth", 0) <= metrics.get("viewportWidth", 0) + 4
	            or metrics.get("scoreCards", 0) >= 10
	        ),
	        "scoreboard sort control": set(["final", "delta", "flow", "bottleneck", "risk", "portfolio", "peer"]).issubset(set(metrics.get("scoreSortOptions") or [])),
	        "map nonblank": metrics.get("mapExists") and metrics.get("mapCircles", 0) >= 15 and metrics.get("mapPaths", 0) >= 10 and metrics.get("mapBoxWidth", 0) >= 260,
        "scenario controls": metrics.get("scenarioControls", 0) >= 7,
        "return attribution": bool(metrics.get("attributionExists")),
        "alert rows": metrics.get("alertRows", 0) >= 1,
        "backtest present": bool(metrics.get("backtestExists")),
        "keyboard focusables": metrics.get("focusableControls", 0) >= 10,
        "table headers scoped": bool(metrics.get("allThScoped")),
    }
    for name, ok in checks.items():
        if not ok:
            failures.append(f"{label}: {name} failed ({metrics})")
    return failures


def run_audit(args) -> int:
    chrome = args.chrome or find_chrome()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_html = read_text(Path(args.dashboard))
    aics = json.loads(read_text(Path(args.aics_json)))
    failures = []

    for name, ok in static_checks(dashboard_html, aics).items():
        if not ok:
            failures.append(f"static check failed: {name}")

    desktop_target = write_temp_target(dashboard_html, "desktop_metrics", True)
    desktop_dom = run_dump(chrome, f"file://{desktop_target}#/portfolio/aics", 1440, 900)
    desktop_metrics = extract_audit_json(desktop_dom)

    mobile_target = write_temp_target(dashboard_html, "mobile_metrics", True)
    mobile_dom = run_dump(chrome, f"file://{mobile_host(mobile_target, 'mobile_metrics', True)}", 520, 900)
    mobile_metrics = extract_audit_json(mobile_dom)

    failures.extend(assert_metrics("desktop", desktop_metrics))
    failures.extend(assert_metrics("mobile", mobile_metrics))
    if mobile_metrics.get("viewportWidth") != 390:
        failures.append(f"mobile: iframe viewport width is {mobile_metrics.get('viewportWidth')}, expected 390")

    desktop_shot_target = write_temp_target(dashboard_html, "desktop_shot", False)
    desktop_png = OUT_DIR / "aics_desktop.png"
    run_screenshot(chrome, f"file://{desktop_shot_target}#/portfolio/aics", 1440, 900, desktop_png)

    mobile_shot_target = write_temp_target(dashboard_html, "mobile_shot", False)
    mobile_png = OUT_DIR / "aics_mobile.png"
    run_screenshot(chrome, f"file://{mobile_host(mobile_shot_target, 'mobile_shot', False)}", 520, 900, mobile_png)

    for label, png, min_width, min_bytes in (
        ("desktop", desktop_png, 1200, 30000),
        ("mobile", mobile_png, 500, 20000),
    ):
        width, height = png_dimensions(png)
        if width < min_width or height < 800 or png.stat().st_size < min_bytes:
            failures.append(f"{label}: screenshot weak ({png}, {width}x{height}, {png.stat().st_size} bytes)")

    result = {
        "status": "fail" if failures else "pass",
        "chrome": chrome,
        "desktop": desktop_metrics,
        "mobile": mobile_metrics,
        "screenshots": {"desktop": str(desktop_png), "mobile": str(mobile_png)},
        "failures": failures,
    }
    result_path = OUT_DIR / "aics_dashboard_audit.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the generated AICS dashboard tab with headless Chrome.")
    parser.add_argument("--dashboard", default=str(DASHBOARD))
    parser.add_argument("--aics-json", default=str(AICS_JSON))
    parser.add_argument("--chrome", default=None)
    return run_audit(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
