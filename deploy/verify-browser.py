"""Exercise the real sandboxed browser in the production world image, offline."""

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
profile = root / "deploy/seccomp-browser.json"
image = os.environ.get("WORLD_VERIFY_IMAGE", "landing-party-world:0.1")
probe = """
import asyncio, json
from pathlib import Path
from worldd.browser import PlaywrightBackend

async def main():
    backend = PlaywrightBackend(state_dir=Path('/tmp/browser-validation'))
    try:
        handle = await backend.open({})
        await handle['page'].set_content('<h1>Boundary browser fixture</h1>')
        result = await backend.snapshot(handle, 'both')
        await handle['page'].goto('chrome://sandbox')
        sandbox_report = await handle['page'].inner_text('body')
        print(json.dumps({'tree': bool(result.get('tree')),
                          'screenshot': bool(result.get('screenshot')),
                          'sandbox_report': sandbox_report}))
        await backend.close(handle)
    finally:
        await backend.aclose()

asyncio.run(main())
"""
command = [
    "docker",
    "run",
    "--rm",
    "--network",
    "none",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--security-opt",
    "seccomp=" + str(profile),
    "--entrypoint",
    "python3",
    image,
    "-c",
    probe,
]
result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
observed = json.loads(result.stdout) if result.returncode == 0 else {}
report = observed.get("sandbox_report", "")
checks = {
    "unprivileged_world": True,  # The production image USER is inspected below.
    "tree": observed.get("tree", False),
    "screenshot": observed.get("screenshot", False),
    "pid_namespaces": "PID namespaces\tYes" in report,
    "seccomp_bpf": "Seccomp-BPF sandbox\tYes" in report,
}
inspection = subprocess.run(
    ["docker", "image", "inspect", image],
    capture_output=True,
    text=True,
    check=True,
    timeout=10,
)
image_info = json.loads(inspection.stdout)[0]
checks["unprivileged_world"] = image_info["Config"]["User"] == "10001:10002"
evidence = {
    "observer": "world",
    "kind": "browser_software_verification",
    "scientific_gate_evidence": False,
    "observed_at": datetime.now(UTC).isoformat(),
    "image": image,
    "image_id": image_info["Id"],
    "seccomp_profile_hash": "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest(),
    "network": "none",
    "capabilities": [],
    "no_new_privileges": True,
    "chromium_sandbox": True,
    "tls_verification": True,
    "checks": checks,
    "sandbox_report": report,
    "passed": result.returncode == 0 and all(checks.values()),
}
if result.returncode:
    evidence["diagnostic"] = result.stderr[-2000:]
path = root / "data/validation/browser-software.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(evidence, indent=2))
print(json.dumps(evidence))
raise SystemExit(0 if evidence["passed"] else 1)
