"""Check a temporary real world image; --live also makes free example.com reads."""

import argparse
import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--live", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
name = "lp-world-verify-" + uuid.uuid4().hex[:8]
env = {**os.environ, "WORLD_CONTROL_TOKEN": secrets.token_urlsafe(32)}
command = [
    "docker",
    "run",
    "-d",
    "--name",
    name,
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--security-opt",
    "seccomp=" + str(root / "deploy/seccomp-browser.json"),
    "--tmpfs",
    "/data:rw,nosuid,uid=10001,gid=10002,size=128m",
    "-e",
    "WORLD_CONTROL_TOKEN",
    "-e",
    "WORLD_WORK_GROUP_WRITABLE=1",
    "-e",
    "NO_PROXY=127.0.0.1,localhost",
]
if args.live:
    command += [
        "-e",
        "HTTP_PROXY",
        "-e",
        "HTTPS_PROXY",
        "-e",
        "WORLD_DOH_URL=https://dns.google/resolve",
        "--add-host",
        "proxy:" + os.environ.get("WORLD_PLATFORM_PROXY_IP", "172.31.14.125"),
    ]
else:
    command += ["--network", "none"]
command.append("landing-party-world:0.1")
probe = """
import json, os, ssl
import httpx
client = httpx.Client(trust_env=False, timeout=75)
control = 'http://127.0.0.1:7777'
headers = {'X-World-Control-Token': os.environ['WORLD_CONTROL_TOKEN']}
checks = {'health': client.get(control+'/healthz').status_code == 200,
          'control_auth': client.get(control+'/v1/runs').status_code == 401,
          'agent_no_control_route': client.get('http://127.0.0.1:7778/v1/runs').status_code == 404}
created = client.post(control+'/v1/run', headers=headers,
    json={'goal':'Verify owned software boundary', 'budget':{'usd':1,'wall_s':180},
          'packs':[], 'raw_lane_enabled': True, 'isolation_profile':'fenced'}).json()
run_id = created['run_id']
agent_headers = {'Authorization': 'Bearer '+created['run_token']}
checks['agent_auth'] = client.get('http://127.0.0.1:7778/v1/status',params={'run_id':run_id}).status_code == 401
checks['agent_capability'] = client.get('http://127.0.0.1:7778/v1/status',headers=agent_headers,params={'run_id':run_id}).status_code == 200
observations = {}
if LIVE:
    fetched = client.post('http://127.0.0.1:7778/v1/act',headers=agent_headers,
        json={'run_id':run_id,'tool':'fetch','params':{'url':'https://example.com/','wait':True}}).json()
    observations['tool_fetch'] = {key:fetched.get(key) for key in ['kind','status','type','rule_id','hash']}
    checks['live_tool_fetch'] = fetched.get('kind') == 'result' and fetched.get('status') == 200
    if not checks['live_tool_fetch']:
        import asyncio
        from pathlib import Path
        from worldd.gateway import Gateway
        from worldd.dns import SecureDNSResolver
        async def diagnostic():
            resolver = SecureDNSResolver('https://dns.google/resolve')
            boundary = Gateway(Path('/data/transport-diagnostic'),resolver=resolver)
            try:
                address, denial = await boundary._target('https://example.com/robots.txt')
                if denial:
                    return {'target_rule':denial.get('rule_id')}
                try:
                    status, headers, body = await boundary._request('https://example.com/robots.txt',address,'diagnostic')
                    return {'target_ip':address,'status':status}
                except Exception as exc:
                    return {'target_ip':address,'error_type':type(exc).__name__,
                            'tls_failure':'certificate' in str(exc).lower() or 'ssl' in str(exc).lower()}
            finally:
                await boundary.aclose()
                await resolver.aclose()
        observations['transport_diagnostic'] = asyncio.run(diagnostic())
    binding = created['proxy']
    proxy_client = httpx.Client(proxy=httpx.Proxy('http://127.0.0.1:8080',auth=(binding['username'],binding['password'])),
        verify=ssl.create_default_context(cafile=binding['ca_path']),trust_env=False,timeout=75)
    try:
        response = proxy_client.get('https://example.com/')
        checks['live_https_proxy'] = response.status_code == 200
        observations['raw_proxy'] = {'status':response.status_code,'bytes':len(response.content)}
    finally:
        proxy_client.close()
sealed = client.post(control+'/v1/beam-up',headers=headers,json={'run_id':run_id,'reason':'harness_stopped'}).json()
checks['sealed'] = sealed.get('state') == 'sealed'
print(json.dumps({'checks':checks,'observations':observations,'passed':all(checks.values())}))
""".replace("LIVE", repr(args.live))
started = False
try:
    subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=20)
    started = True
    for _ in range(60):
        healthy = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "python3",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7777/healthz',timeout=1).read()",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        if healthy.returncode == 0:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("World image did not become healthy")
    result = subprocess.run(
        ["docker", "exec", name, "python3", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=170,
    )
    evidence = (
        json.loads(result.stdout)
        if result.returncode == 0
        else {"passed": False, "error": "world_probe_failed"}
    )
    inspection = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "landing-party-world:0.1"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    evidence.update(
        observer="world",
        kind="world_software_verification",
        scientific_gate_evidence=False,
        observed_at=datetime.now(UTC).isoformat(),
        image_id=inspection.stdout.strip(),
        live=args.live,
    )
    path = root / (
        "data/validation/world-live-software.json"
        if args.live
        else "data/validation/world-software.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))
finally:
    if started:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, check=False, timeout=20
        )
raise SystemExit(0 if evidence["passed"] else 1)
