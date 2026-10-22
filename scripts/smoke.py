"""Exercise a running local worldd without external network requests or paid APIs."""

import argparse
import io
import json
import os
import tarfile
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7777")
    parser.add_argument("--agent-url", default="http://127.0.0.1:7778")
    args = parser.parse_args()
    # Local control traffic must not be sent through a cloud egress proxy.
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None, *, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if os.environ.get("WORLD_CONTROL_TOKEN"):
            headers["X-World-Control-Token"] = os.environ["WORLD_CONTROL_TOKEN"]
        req = Request(args.url.rstrip("/") + path, data=data, headers=headers)
        with opener.open(req, timeout=10) as response:
            result = response.read()
        return result if raw else json.loads(result)

    health = request("/healthz")
    if health.get("status") != "ok":
        raise RuntimeError("worldd health check failed")
    info = request(
        "/v1/run",
        {
            "goal": "Local development smoke",
            "packs": ["deliver"],
            "budget": {"usd": 1, "wall_s": 60},
        },
    )
    run_id = info["run_id"]
    agent_request = Request(
        args.agent_url.rstrip("/") + f"/v1/status?run_id={run_id}",
        headers={"Authorization": f"Bearer {info['run_token']}"},
    )
    with opener.open(agent_request, timeout=10) as response:
        state = json.loads(response.read())
    if state.get("run_id") != run_id or "tuple" in state:
        raise RuntimeError("run-scoped agent listener returned an invalid status")

    def act(tool, **params):
        return request("/v1/act", {"run_id": run_id, "tool": tool, "params": params})

    try:
        if act("deliver", kind="smoke", body="local artifact")["kind"] != "silence":
            raise RuntimeError("deliver failed")
        if act("ask", kind="message", text="recorded smoke message")["kind"] != "silence":
            raise RuntimeError("ask failed")
        denied = act("fetch", url="http://169.254.169.254/latest/meta-data")
        if denied.get("rule_id") != "private_destination":
            raise RuntimeError("private destination policy failed")
        if act("status")["spent"]:
            raise RuntimeError("free tools unexpectedly charged the ledger")
    finally:
        request("/v1/beam-up", {"run_id": run_id})
    report = request(f"/v1/report?run_id={run_id}")
    integrity = report["integrity"]
    if not integrity["record"]["valid"] or not integrity["ledger"]["valid"] or integrity["tainted"]:
        raise RuntimeError("sealed record or ledger verification failed")
    episode = request(f"/v1/export?run_id={run_id}", raw=True)
    with tarfile.open(fileobj=io.BytesIO(episode), mode="r:gz") as archive:
        if not any("work-snapshot/out/" in name for name in archive.getnames()):
            raise RuntimeError("workspace snapshot missing from export")
    print(
        json.dumps(
            {
                "status": "passed",
                "run_id": run_id,
                "checks": [
                    "health",
                    "agent_capability",
                    "deliver",
                    "ask",
                    "private_destination_policy",
                    "free_tools",
                    "seal",
                    "record",
                    "ledger",
                    "snapshot",
                    "export",
                ],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (URLError, RuntimeError, KeyError, ValueError) as exc:
        raise SystemExit(f"smoke failed: {exc}") from None
