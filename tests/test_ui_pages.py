"""UI regression: every page is served, shares the one design system, and stays reachable from the navigation."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = {"/top-bottom": "top_bottom.html", "/journal": "top_bottom.html", "/simulator": "simulator.html",
         "/builder": "builder.html", "/backtesting": "builder.html", "/history": "builder.html", "/options": "options.html",
         "/settings": "settings.html"}
NAV_ROUTES = ["/top-bottom", "/simulator", "/builder", "/backtesting", "/history", "/options", "/settings"]


def test_home_redirects_to_top_bottom(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/top-bottom"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)          # no lifespan: no broker session, no background engine


@pytest.mark.parametrize("route", PAGES)
def test_page_is_served(client, route):
    r = client.get(route)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("name", sorted(set(PAGES.values())))
def test_page_uses_shared_theme_and_shell(name):
    html = (WEB / name).read_text(encoding="utf-8")
    assert "/static/theme.css" in html and "/static/shell.js" in html and "data-shell" in html and 'name="viewport"' in html
    assert "fonts.googleapis.com" not in html                                     # no runtime dependency on an external font host


def test_navigation_lists_every_route_exactly_once():
    js = (WEB / "shell.js").read_text(encoding="utf-8")
    block = js[js.index("var NAV"):js.index("document.addEventListener")]
    hrefs = re.findall(r'"(/[a-z0-9-]*)"\]', block)
    assert sorted(hrefs) == sorted(NAV_ROUTES) and len(hrefs) == len(set(hrefs))


def test_navigation_is_flat_without_dropdowns():
    js = (WEB / "shell.js").read_text(encoding="utf-8")
    assert "nav-grp" not in js and "aria-haspopup" not in js


def test_market_and_paper_status_are_in_the_shell():
    js = (WEB / "shell.js").read_text(encoding="utf-8")
    assert 'id="nav-mkt"' in js and "no orders" in js and "/api/overview" in js


def test_design_tokens_and_self_hosted_fonts():
    css = (WEB / "theme.css").read_text(encoding="utf-8")
    for token in ("--sp-1", "--ctl-h", "--row-h", "--font-mono", "--chart-h", "--r-md", "prefers-reduced-motion"):
        assert token in css
    assert "fonts.googleapis.com" not in css and "/static/fonts/inter.css" in css
    fonts = (WEB / "fonts" / "inter.css").read_text(encoding="utf-8")
    for fam in ("Inter", "IBM Plex Mono"):
        assert fam in fonts
    for f in re.findall(r"/static/fonts/([\w.-]+\.woff2)", fonts):
        assert (WEB / "fonts" / f).stat().st_size > 5000, f


def test_single_column_breakpoints_can_shrink():
    css = (WEB / "theme.css").read_text(encoding="utf-8")
    assert "grid-template-columns: minmax(0, 1fr)" in css                         # plain 1fr caused horizontal overflow at 900 px


def test_top_bottom_page_has_compact_layout_pieces():
    html = (WEB / "top_bottom.html").read_text(encoding="utf-8")
    for needle in ('id="sum"', 'id="stats"', 'id="trades"', 'id="ctype"', "clamp(", "grid-template-columns: minmax(0, 1fr) 320px"):
        assert needle in html


import shutil
import subprocess
import tempfile


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", sorted(set(PAGES.values())))
def test_inline_scripts_have_valid_javascript_syntax(name, tmp_path):
    """A single stray character in a page's <script> silently kills the whole page (it just sits on 'connecting...')."""
    html = (WEB / name).read_text(encoding="utf-8")
    for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        f = tmp_path / f"{name}.{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} script #{i}: {r.stderr.splitlines()[:4]}"


def test_shell_js_has_valid_syntax(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    r = subprocess.run(["node", "--check", str(WEB / "shell.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
