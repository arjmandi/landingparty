"""The control console's CSP must match exactly the script that executes."""

import base64
import hashlib
import re
from pathlib import Path


def test_bridge_script_has_valid_content_security_hash():
    source = (Path(__file__).parents[1] / "worldd/console.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", source, re.DOTALL)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in source
    assert "default-src 'none'" in source and "connect-src 'self'" in source
    assert "form-action 'none'" in source and "base-uri 'none'" in source
    assert 'referrer" content="no-referrer' in source
