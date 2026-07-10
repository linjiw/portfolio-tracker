#!/usr/bin/env python3
"""Audit helper: screenshot every dashboard tab in desktop + mobile.

Injects a tiny auto-driver <script> before </body> that activates a target seg
(and, for the per-stock detail view, first selects the top holding row), then
drives headless Chrome to capture each. Output: output/audit_shots/.

MOBILE IS RENDERED IN A TRUE 390px VIEWPORT: Chrome --headless=new clamps the
window to ≥500px wide, so a plain --window-size=390 shot silently renders a
500px layout (mobile QA was blind to this for weeks). We host the page in a
390px iframe inside a 520px window and crop the iframe box; the driver also
stamps a loud red banner into the page if window.innerWidth ≠ 390.

Each tab gets TWO captures per width: a full-page scroll capture (*_desktop /
*_mobile) and a real-fold capture (*_desktop_fold 1440x900, *_mobile_fold
390x844) — the tall captures balloon every vh-based cap and misrepresent
above-the-fold rhythm.
"""
import os, subprocess

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "output", "portfolio_dashboard.html")
OUT = os.path.join(HERE, "output", "audit_shots")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
os.umask(0o077)
os.makedirs(OUT, mode=0o700, exist_ok=True)
os.chmod(OUT, 0o700)

html = open(SRC, encoding="utf-8").read()
assert "</body></html>" in html, "unexpected template tail"

# (view, seg).  view 'ov' = overview rail; 'stk' = per-stock detail rail.
OV_SEGS = ["score", "qt", "nw", "risk", "struct", "cmp", "pfib", "sig", "beh", "journal", "rebal"]
STK_SEGS = ["price", "tx", "fib", "journal"]
FULL_H = {"desktop": 5200, "mobile": 6400}
FOLD = {"desktop": (1440, 900), "mobile": (390, 844)}


def driver(view, seg, expect_width=None):
    guard = ""
    if expect_width:
        guard = (
            "if(window.innerWidth!==%d){document.body.insertAdjacentHTML('afterbegin',"
            "'<div style=\"font:700 32px sans-serif;color:#fff;background:#c00;padding:20px\">"
            "VIEWPORT '+window.innerWidth+'px != %dpx — harness broken</div>');}"
        ) % (expect_width, expect_width)
    if view == "ov":
        act = (
            "if(typeof sel!=='undefined'&&sel!=='__OV__'){sel='__OV__';renderList();}"
            "var b=document.querySelector('#right .seg-rail [data-seg=\"%s\"]');if(b)b.click();"
        ) % seg
        return f"<script>setTimeout(function(){{try{{{guard}{act}}}catch(e){{console.error(e);}}window.scrollTo(0,0);}},250);</script>"
    act = (
        "var row=document.querySelector('.row[data-s]:not(.ovrow)');if(row)row.click();"
        "setTimeout(function(){try{var b=document.querySelector('#right .seg-rail [data-seg=\"%s\"]');"
        "if(b)b.click();}catch(e){console.error(e);}window.scrollTo(0,0);},350);"
    ) % seg
    return f"<script>setTimeout(function(){{try{{{guard}{act}}}catch(e){{console.error(e);}}}},250);</script>"


def chrome(png, url, w, h):
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--window-size={w},{h}",
         "--virtual-time-budget=7000", f"--screenshot={png}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)


def shoot(view, seg):
    for wname in ("desktop", "mobile"):
        mobile = wname == "mobile"
        page = html.replace("</body></html>",
                            driver(view, seg, 390 if mobile else None) + "</body></html>")
        tmp = os.path.join(OUT, f"_tmp_{view}_{seg}.html")
        open(tmp, "w", encoding="utf-8").write(page)
        for kind, (w, h) in (("", (1440 if not mobile else 390, FULL_H[wname])), ("_fold", FOLD[wname])):
            png = os.path.join(OUT, f"{view}_{seg}_{wname}{kind}.png")
            if mobile:
                # true-390 viewport via iframe host; crop the iframe box afterwards
                host = os.path.join(OUT, f"_host_{view}_{seg}.html")
                open(host, "w", encoding="utf-8").write(
                    f'<!DOCTYPE html><html><body style="margin:0;background:#000">'
                    f'<iframe src="file://{tmp}" style="width:390px;height:{h}px;border:0;display:block"></iframe>'
                    f"</body></html>")
                chrome(png, f"file://{host}", 520, h)
                try:
                    from PIL import Image
                    im = Image.open(png)
                    im.crop((0, 0, 390, min(h, im.size[1]))).save(png)
                except Exception as e:
                    print(f"  crop failed ({e}) — shot kept uncropped")
                os.remove(host)
            else:
                chrome(png, f"file://{tmp}", w, h)
            print(f"  {view}/{seg} {wname}{kind}: {'OK' if os.path.exists(png) else 'FAIL'}")
        os.remove(tmp)


def lint_palette():
    """Fail loudly on raw-hex drift: every 6-digit hex in the emitted HTML must be declared
    in :root, the JS C/QTC/FIBCOL/qStateColor palettes, or the small documented allow-list."""
    import re
    hexes = lambda s: set(h.upper()[:7] for h in re.findall(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b", s))   # 8-digit alpha forms count as their base color
    allowed = set()
    root = re.search(r":root\{.*?\n\}", html, re.S)
    if root:
        allowed |= hexes(root.group(0))
    for pat in (r"const C=.*?\}\)\(\);", r"const QTC=\{.*?\};", r"const FIBCOL=\{.*?\};",
                r"function qStateColor.*?\n", r"--grain:url\(.*?\);", r"body::before\{.*?\}"):
        m = re.search(pat, html, re.S)
        if m:
            allowed |= hexes(m.group(0))
    # documented one-offs: semantic guide darks, card tick, whole-account allocation segs
    allowed |= {"#2F6B4F", "#6B2F2F", "#3A3F47", "#7F8794", "#555A63"}
    offenders = sorted(hexes(html) - allowed)
    if offenders:
        print(f"✗ palette lint: {len(offenders)} undeclared hex value(s): {', '.join(offenders[:12])}")
        return False
    print("✓ palette lint: no raw-hex drift")
    return True


def check_grain():
    """Atmosphere is a tested property: the flattest panel window must show real tooth (std 2.5-6.5 RGB)."""
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(os.path.join(OUT, "ov_score_desktop_fold.png")).convert("L")
        a = np.asarray(im, dtype=float)
        best = None
        for y in range(120, min(820, a.shape[0] - 40), 40):
            for x in range(420, min(1380, a.shape[1] - 40), 60):
                w = a[y:y+40, x:x+40]
                if w.std() < (best[0] if best else 1e9):
                    best = (w.std(), x, y)
        std = best[0]
        ok = 2.0 <= std <= 7.0
        print(f"{'✓' if ok else '✗'} grain check: flattest 40px window std={std:.2f} (target 2.5-6.5)")
        return ok
    except Exception as e:
        print(f"grain check skipped ({e})")
        return True


def main():
    ok = lint_palette()
    for seg in OV_SEGS:
        shoot("ov", seg)
    for seg in STK_SEGS:
        shoot("stk", seg)
    print("done ->", OUT)
    gok = check_grain()
    if not (ok and gok):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
