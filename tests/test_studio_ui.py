"""Product shell safety and packaging contracts; browser flows live in studio_smoke.py."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_studio_uses_same_origin_assets_and_has_accessible_navigation():
    html = (ROOT / "worldd/studio.html").read_text()
    assert "script-src 'self'" in html and "connect-src 'self'" in html
    assert "default-src 'none'" in html and "base-uri 'none'" in html
    assert re.findall(r'<script[^>]+src="([^"]+)"', html) == ["/assets/studio.js"]
    assert re.findall(r'<link[^>]+href="([^"]+)"', html) == ["/assets/studio.css"]
    assert all(
        not script.strip() for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    )
    assert not re.search(r"\son\w+\s*=", html)
    assert "Skip to content" in html
    for page in ("runs", "harnesses", "beliefs", "asks", "footprint", "settings"):
        assert f'data-page="{page}"' in html
    assert 'aria-labelledby="new-run-title"' in html
    assert 'aria-labelledby="login-title"' in html


def test_studio_secret_and_untrusted_content_boundaries():
    source = (ROOT / "worldd/studio.js").read_text()
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "eval(",
        "x-world-control-token",
    ):
        assert forbidden not in source
    assert "credentials:'same-origin'" in source
    assert "input.value=''" in source
    assert "api('/v1/asks')" not in source  # reading the product view is passive
    assert "'/api/runs/'+encodeURIComponent(id)+'/asks'" in source
    assert "api('/v1/acts'" not in source
    assert "Model dollars are unavailable" in source
    assert "Use a dedicated host or OS user" in source
    assert "item.auth_modes.includes(mode?.id)" in source
    assert "$('#run-model').required=!subscription" in source
    assert "packs.push('schedule')" not in source


def test_studio_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js syntax checker is unavailable")
    subprocess.run(
        [node, "--check", str(ROOT / "worldd/studio.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_subscription_state_requires_active_flow_and_valid_link_before_code():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js state regression checker is unavailable")
    source = (ROOT / "worldd/studio.js").read_text()
    helper = source[
        source.index("function authPresentation(") : source.index("function authFailure(")
    ]
    official = "https://claude.com/cai/oauth/authorize?client_id=fixture&code=true"
    cases = [
        {"state": "disconnected", "requires_code": True, "auth_url": official},
        {"state": "connecting", "requires_code": True},
        {"state": "connecting", "requires_code": True, "auth_url": official},
        {"state": "awaiting_code", "auth_url": official},
        {"state": "failed", "requires_code": True, "auth_url": official},
        {"state": "connecting", "requires_code": True, "auth_url": "javascript:alert(1)"},
        {
            "state": "connecting",
            "requires_code": True,
            "auth_url": "https://user:secret@claude.com/cai/oauth/authorize",
        },
        {"state": "connected", "requires_code": True, "auth_url": official},
    ]
    script = (
        helper + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(authPresentation)))"
    )
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    rows = json.loads(result.stdout)
    assert [row["state"] for row in rows] == [
        "disconnected",
        "connecting",
        "connecting",
        "connecting",
        "failed",
        "connecting",
        "connecting",
        "connected",
    ]
    assert [row["manualCode"] for row in rows] == [
        False,
        False,
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert [row["loginUrl"] for row in rows] == [
        None,
        None,
        official,
        official,
        None,
        None,
        None,
        None,
    ]
