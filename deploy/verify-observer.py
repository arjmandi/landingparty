"""Exercise the signed host bridge against a real Docker-free world container."""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from worldd.host_observer import observe_control
from worldd.util import digest


async def main(*, packaged=False):
    identity = "lp-observer-" + uuid.uuid4().hex[:8]
    network, volume, world, fence, agent = (
        identity + suffix for suffix in ("-net", "-data", "-world", "-fence", "-agent")
    )
    control_network = identity + "-control"
    token = secrets.token_urlsafe(32)
    environment = {**os.environ, "WORLD_CONTROL_TOKEN": token}
    created = []

    def docker(*argv, check=True):
        result = subprocess.run(
            ["docker", *argv], env=environment, capture_output=True, text=True, check=False
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr[:1500])
        return result

    with tempfile.TemporaryDirectory(prefix="lp-observer-source-") as folder:
        source = Path(folder) / "worldd"
        shutil.copytree("worldd", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in [source, *source.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        try:
            docker("network", "create", "--internal", "--subnet", "172.30.77.0/24", network)
            docker("network", "create", control_network)
            docker("volume", "create", volume)
            docker(
                "run",
                "-d",
                "--name",
                world,
                "--network",
                control_network,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--security-opt",
                "seccomp=" + str(Path("deploy/seccomp-browser.json").resolve()),
                "--mount",
                f"type=volume,src={volume},dst=/data",
                *(
                    []
                    if packaged
                    else [
                        "--mount",
                        f"type=bind,src={source},dst=/opt/landingparty/worldd,readonly",
                    ]
                ),
                "-e",
                "WORLD_CONTROL_TOKEN",
                "-e",
                "WORLD_WORK_GROUP_WRITABLE=1",
                "-e",
                "NO_PROXY=127.0.0.1,localhost",
                "-p",
                "127.0.0.1::7777",
                "landing-party-world:0.1",
            )
            created.append(world)
            docker("network", "connect", "--ip", "172.30.77.2", network, world)
            port = json.loads(docker("inspect", world).stdout)[0]["NetworkSettings"]["Ports"][
                "7777/tcp"
            ][0]["HostPort"]
            url = "http://127.0.0.1:" + port
            async with httpx.AsyncClient(
                base_url=url, trust_env=False, timeout=90, headers={"X-World-Control-Token": token}
            ) as client:
                for _ in range(60):
                    try:
                        response = await client.get("/healthz")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError(docker("logs", world, check=False).stderr[-1500:])
                created_run = await client.post(
                    "/v1/run",
                    json={
                        "budget": {"usd": 1, "wall_s": 180},
                        "packs": [],
                        "stage": 0,
                        "research_mode": "research",
                        "model_snapshot_id": "gpt-4o-2024-08-06",
                        "isolation_profile": "fenced",
                        "harness_id": "manual@0.1",
                    },
                )
                created_run.raise_for_status()
                run_id = created_run.json()["run_id"]
                docker(
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
                    "--label",
                    "landing-party.run_id=" + run_id,
                    "--label",
                    "landing-party.role=fence",
                    "-e",
                    "WORLD_INTERNAL_IP=172.30.77.2",
                    "landing-party-agent:0.1",
                    "sh",
                    "/opt/landingparty/deploy/init-fence.sh",
                )
                created.append(fence)
                for _ in range(30):
                    if (
                        docker(
                            "exec", fence, "test", "-f", "/tmp/fence-ready", check=False
                        ).returncode
                        == 0
                    ):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Fence initialization failed")
                docker(
                    "run",
                    "-d",
                    "--name",
                    agent,
                    "--network",
                    "container:" + fence,
                    "--user",
                    "10002:10002",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,size=16m",
                    "--label",
                    "landing-party.run_id=" + run_id,
                    "--label",
                    "landing-party.role=agent",
                    "--mount",
                    f"type=volume,src={volume},dst=/work,volume-subpath=runs/{run_id}/work",
                    "--mount",
                    f"type=volume,src={volume},dst=/world,volume-subpath=runs/{run_id}/world,readonly",
                    "-e",
                    "WORLD_RUN_ID=" + run_id,
                    "-e",
                    "HTTP_PROXY=http://172.30.77.2:8080",
                    "-e",
                    "HTTPS_PROXY=http://172.30.77.2:8080",
                    "-e",
                    "OPENAI_BASE_URL=http://172.30.77.2:8081/openai/v1",
                    "-e",
                    "SSL_CERT_FILE=/world/ca.pem",
                    "landing-party-agent:0.1",
                    "sleep",
                    "infinity",
                )
                created.append(agent)
                report = await observe_control(url, run_id, agent, fence, token=token, sources={})
                checked = report["checks"]
                browser_run = await client.post(
                    "/v1/run",
                    json={"budget": {"usd": 1, "wall_s": 180}, "packs": ["browse"], "stage": 2},
                )
                browser_run.raise_for_status()
                source_observation = await client.post(
                    "/v1/preflight/observe", json={"run_id": browser_run.json()["run_id"]}
                )
                source_observation.raise_for_status()
                artifact_path = source_observation.json()["metadata"]["versions"]["path"]
                versions = json.loads(docker("exec", world, "cat", artifact_path).stdout)
                inspect_world = json.loads(docker("inspect", world).stdout)[0]
                checkout_hash = digest(
                    {
                        str(path.relative_to(source)): digest(path.read_bytes())
                        for path in sorted(source.rglob("*"))
                        if path.is_file()
                    }
                )
                checks = {
                    name: checked[name]["status"] == "passed"
                    for name in (
                        "harness_environment",
                        "fenced_egress",
                        "evaluation_isolation",
                        "initial_surfaces",
                        "kill_switch",
                    )
                }
                checks.update(
                    {
                        "signed_receipt_bound_to_report": bool(
                            report.get("host_observer", {}).get("receipt_hash")
                        ),
                        "research_capability_withheld": created_run.json()["state"] == "preflight"
                        and created_run.json().get("run_token") is None,
                        "research_remains_preflight": (
                            await client.get("/v1/status", params={"run_id": run_id})
                        ).json()["state"]
                        == "preflight",
                        "unmeasured_research_activation_denied": (
                            await client.post(f"/v1/run/{run_id}/activate")
                        ).status_code
                        == 400,
                        "actual_browser_version_observed": versions["browser_build"].startswith(
                            "Chromium "
                        )
                        and versions["browser_observation"]["status"] == "measured",
                        "missing_scientific_sources_stay_unmeasured": not report["passed"]
                        and all(
                            name in report["unmeasured"]
                            for name in ("egress", "provider_canary", "block_profile", "versions")
                        ),
                        "world_has_no_docker_client": docker(
                            "exec", world, "sh", "-c", "command -v docker", check=False
                        ).returncode
                        != 0,
                        "world_has_no_docker_socket": not any(
                            "docker.sock" in mount["Destination"]
                            for mount in inspect_world["Mounts"]
                        ),
                        "unauthenticated_observer_denied": (
                            await client.post(
                                "/v1/preflight/observer/challenge",
                                headers={"X-World-Control-Token": "wrong"},
                                json={},
                            )
                        ).status_code
                        == 401,
                    }
                )
                if packaged:
                    actual_hash = docker(
                        "exec",
                        world,
                        "python3",
                        "-c",
                        "from pathlib import Path; from worldd.util import digest; "
                        "root=Path('/opt/landingparty/worldd'); "
                        "print(digest({str(p.relative_to(root)):digest(p.read_bytes()) "
                        "for p in sorted(root.rglob('*')) if p.is_file() "
                        "and '__pycache__' not in p.parts and p.suffix!='.pyc'}))",
                    ).stdout.strip()
                    checks["packaged_source_matches_checkout"] = actual_hash == checkout_hash
                outcome = {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "checks": checks,
                    "check_count": len(checks),
                    "passed": all(checks.values()),
                    "scientific_gate_evidence": False,
                    "source_mounted_readonly": not packaged,
                    "source_manifest_hash": checkout_hash,
                    "world_image_id": inspect_world["Image"],
                    "report_hash": report["report_hash"],
                }
                target = Path("data/validation/observer-software.json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(outcome, indent=2) + "\n")
                print(json.dumps(outcome))
                if not outcome["passed"]:
                    raise SystemExit(1)
        finally:
            for name in reversed(created):
                docker("rm", "-f", name, check=False)
            docker("volume", "rm", volume, check=False)
            docker("network", "rm", network, check=False)
            docker("network", "rm", control_network, check=False)


if __name__ == "__main__":
    os.environ.setdefault("DOCKER_CONFIG", "/workspace/.cache/docker")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packaged", action="store_true", help="use only the actual image source")
    asyncio.run(main(packaged=parser.parse_args().packaged))
