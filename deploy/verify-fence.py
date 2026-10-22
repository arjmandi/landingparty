import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

suffix = uuid.uuid4().hex[:8]
network = "lp-fence-" + suffix
world = "lp-world-" + suffix
fence = "lp-fence-ns-" + suffix
agent = "lp-agent-" + suffix
env = os.environ.copy()


def run(*args, check=True):
    result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr[:1000])
    return result


created = []
try:
    run("network", "create", "--internal", "--subnet", "172.30.77.0/24", network)
    run(
        "run",
        "-d",
        "--name",
        world,
        "--network",
        network,
        "--ip",
        "172.30.77.2",
        "--cap-drop",
        "ALL",
        "landing-party-agent:0.1",
        "python3",
        "-c",
        "from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler; import threading; "
        "threading.Thread(target=ThreadingHTTPServer(('0.0.0.0',7777),SimpleHTTPRequestHandler).serve_forever,daemon=True).start(); "
        "ThreadingHTTPServer(('0.0.0.0',7778),SimpleHTTPRequestHandler).serve_forever()",
    )
    created.append(world)
    run(
        "run",
        "-d",
        "--name",
        fence,
        "--user",
        "0:0",
        "--network",
        network,
        "--ip",
        "172.30.77.3",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "WORLD_INTERNAL_IP=172.30.77.2",
        "landing-party-agent:0.1",
        "sh",
        "/opt/landingparty/deploy/init-fence.sh",
    )
    created.append(fence)
    for _ in range(20):
        if run("exec", fence, "test", "-f", "/tmp/fence-ready", check=False).returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(run("logs", fence, check=False).stdout)
    run(
        "run",
        "-d",
        "--name",
        agent,
        "--network",
        "container:" + fence,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10002:10002",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=16m",
        "landing-party-agent:0.1",
        "sleep",
        "infinity",
    )
    created.append(agent)

    def inspect():
        table = run("exec", fence, "iptables", "-nvx", "-L", "OUTPUT").stdout
        first = table.splitlines()[0]
        import re

        drops = int(re.search(r"policy DROP (\d+) packets", first)[1])
        caps = (
            run(
                "exec",
                agent,
                "python3",
                "-c",
                "from pathlib import Path; print(next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:')))",
            )
            .stdout.strip()
            .split()[-1]
        )
        return {
            "output_policy": "DROP" if "policy DROP" in first else "UNKNOWN",
            "drop_packets": drops,
            "harness_net_admin": bool(int(caps, 16) & (1 << 12)),
        }

    before = inspect()
    checks = {
        "control_fixture_listening": run(
            "exec",
            world,
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "-sS",
            "http://127.0.0.1:7777/",
            check=False,
        ).returncode
        == 0
    }
    hidden = [
        "analysis",
        "probes",
        "prereg",
        "goals",
        "tests",
        "docs",
        "policy",
        ".github",
        "00-landing-party.md",
        "worldd/canaries.py",
        "worldd/runtime.py",
        "worldd/console.html",
        "worldd/skill_tests.py",
    ]
    checks["evaluation_material_unreadable"] = (
        run(
            "exec",
            agent,
            "python3",
            "-c",
            "import os,sys; root='/opt/landingparty/'; sys.exit(1 if any(os.access(root+p,os.R_OK) for p in "
            + repr(hidden)
            + ") else 0)",
            check=False,
        ).returncode
        == 0
    )
    checks["agent_client_imports"] = (
        run(
            "exec",
            agent,
            "python3",
            "-c",
            "import worldd.cli,worldd.mcp,worldd.models; from adapters.openhands.proxy import create_proxy_app",
            check=False,
        ).returncode
        == 0
    )
    checks["agent_cli_available"] = run("exec", agent, "lp", "--help", check=False).returncode == 0
    checks["only_client_modules_present"] = (
        run(
            "exec",
            agent,
            "python3",
            "-c",
            "from pathlib import Path; import sys; root=Path('/opt/landingparty/worldd'); "
            "expected={'__init__.py','cli.py','mcp.py','models.py','util.py'}; "
            "sys.exit(0 if {str(p.relative_to(root)) for p in root.rglob('*.py')} == expected else 1)",
            check=False,
        ).returncode
        == 0
    )
    tests = {
        "world_tool_reachable": [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "-fsS",
            "http://172.30.77.2:7778/",
        ],
        "control_plane_blocked": [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "-sS",
            "http://172.30.77.2:7777/",
        ],
        "direct_curl_blocked": [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "--connect-timeout",
            "1",
            "-I",
            "https://1.1.1.1/",
        ],
        "direct_python_blocked": [
            "python3",
            "-c",
            "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('https://1.1.1.1/',timeout=1).read(1)",
        ],
        "direct_dns_blocked": [
            "python3",
            "-c",
            "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(1); s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('1.1.1.1',53)); s.recvfrom(512)",
        ],
        "agent_cannot_change_firewall": ["iptables", "-P", "OUTPUT", "ACCEPT"],
    }
    for label, command in tests.items():
        result = run("exec", agent, *command, check=False)
        checks[label] = (
            (result.returncode == 0) if label == "world_tool_reachable" else result.returncode != 0
        )
    after = inspect()
    checks["default_drop"] = after["output_policy"] == "DROP"
    checks["drop_counter_increased"] = after["drop_packets"] > before["drop_packets"]
    checks["unprivileged_agent"] = not after["harness_net_admin"]
    outcome = {
        "observer": "world",
        "kind": "fenced_software_verification",
        "observed_at": datetime.now(UTC).isoformat(),
        "scientific_gate_evidence": False,
        "checks": checks,
        "before": before,
        "after": after,
        "passed": all(checks.values()),
        "image": "landing-party-agent:0.1",
        "image_id": run(
            "image", "inspect", "--format", "{{.Id}}", "landing-party-agent:0.1"
        ).stdout.strip(),
        "firewall_script_hash": "sha256:"
        + hashlib.sha256(
            (Path(__file__).resolve().parent / "init-fence.sh").read_bytes()
        ).hexdigest(),
        "world_fixture": "owned HTTP service; no internet calls succeeded",
    }
    path = Path(__file__).resolve().parents[1] / "data/validation/fenced-software.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outcome, indent=2))
    print(json.dumps(outcome))
finally:
    for name in reversed(created):
        run("rm", "-f", name, check=False)
    run("network", "rm", network, check=False)

raise SystemExit(0 if outcome["passed"] else 1)
