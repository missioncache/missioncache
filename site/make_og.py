"""Render the social preview card (site/img/og.jpg, 1200x630) with a headless browser.

Run after changing the headline or the logo:  uv run --with playwright python site/make_og.py
(needs `playwright install chromium` once). The card is served as /og.jpg, never inlined.
"""
from __future__ import annotations

import base64
import pathlib
import re

SITE = pathlib.Path(__file__).resolve().parent
LOGO = base64.b64encode((SITE / "img" / "logo_white.png").read_bytes()).decode()

# The card says what the page says: headline and lede come from the og: metas in the template.
_TPL = (SITE / "landing.template.html").read_text()
_TITLE = re.search(r'property="og:title" content="MissionCache - ([^"]+)"', _TPL).group(1)
_LEDE = re.search(r'property="og:description" content="([^"]+)"', _TPL).group(1)
_LINE1, _LINE2 = _TITLE.split(". ", 1)

HTML = f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;width:1200px;height:630px;background:#070A14;color:#EEF2FF;font:500 16px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;position:relative;overflow:hidden}}
.a{{position:absolute;border-radius:50%;filter:blur(90px);opacity:.6}}
.a1{{width:700px;height:700px;left:-220px;top:-260px;background:#8B5CF6}}
.a2{{width:600px;height:600px;right:-160px;top:120px;background:#3B82F6}}
.w{{position:absolute;inset:0;padding:72px 84px;display:flex;flex-direction:column;justify-content:space-between}}
img{{height:58px;width:auto;align-self:flex-start}}
h1{{margin:0;font-size:66px;line-height:1.05;letter-spacing:-.03em;font-weight:850}}
h1 span{{background:linear-gradient(90deg,#8B5CF6,#3B82F6 60%,#67E8F9);-webkit-background-clip:text;color:transparent}}
p{{margin:18px 0 0;font-size:26px;color:#C3CBE4;max-width:900px;line-height:1.35}}
.f{{display:flex;justify-content:space-between;align-items:center;font:500 20px "SF Mono",ui-monospace,Menlo,monospace;color:#8390B3}}
.f b{{color:#EEF2FF;font-weight:600}}
</style><body><i class="a a1"></i><i class="a a2"></i><div class="w">
<img src="data:image/png;base64,{LOGO}" alt="">
<div><h1>{_LINE1}.<br><span>{_LINE2}</span></h1>
<p>{_LEDE}</p></div>
<div class="f"><b>$ uvx missioncache-install</b><span>missioncache.dev &middot; MIT &middot; local-first</span></div>
</div></body>"""


def main() -> None:
    from playwright.sync_api import sync_playwright

    out = SITE / "img" / "og.jpg"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=1)
        page.set_content(HTML)
        page.screenshot(path=str(out), type="jpeg", quality=88)
        browser.close()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
