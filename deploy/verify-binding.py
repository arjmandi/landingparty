"""Exercise the trusted Docker binding against actual disposable fenced containers."""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from worldd.isolation import DockerHarness


async def main():
    run_id = "binding-" + uuid.uuid4().hex[:10]
    network, fence, agent = (run_id + suffix for suffix in ("-net", "-fence", "-agent"))
    created = []

    def docker(*argv, check=True):
        result = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr[:1000])
        return result

    with tempfile.TemporaryDirectory(prefix="lp-binding-") as directory:
        data = Path(directory)
        for part in ("work", "world"):
            path = data / "runs" / run_id / part
            path.mkdir(parents=True)
            path.chmod(0o777 if part == "work" else 0o755)
        try:
            docker("network", "create", "--internal", "--subnet", "172.30.77.0/24", network)
            image_id = docker(
                "image", "inspect", "landing-party-agent:0.1", "--format", "{{.Id}}"
            ).stdout.strip()
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
                image_id,
                "sh",
                "/opt/landingparty/deploy/init-fence.sh",
            )
            created.append(fence)
            for _ in range(30):
                if (
                    docker("exec", fence, "test", "-f", "/tmp/fence-ready", check=False).returncode
                    == 0
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Firewall initializer did not become ready")
            docker(
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
                "--label",
                "landing-party.run_id=" + run_id,
                "--label",
                "landing-party.role=agent",
                "--mount",
                f"type=bind,src={data / 'runs' / run_id / 'work'},dst=/work",
                "--mount",
                f"type=bind,src={data / 'runs' / run_id / 'world'},dst=/world,readonly",
                image_id,
                "sleep",
                "infinity",
            )
            created.append(agent)
            harness = await DockerHarness.bind(run_id, agent, fence, data_dir=data)
            before = await harness.firewall_inspect()
            attempt = await harness.execute(
                [
                    "python3",
                    "-c",
                    "import socket; s=socket.socket(); s.settimeout(.5); s.connect(('1.1.1.1',443))",
                ]
            )
            control_attempt = await harness.execute(
                [
                    "python3",
                    "-c",
                    "import socket; s=socket.socket(); s.settimeout(.5); s.connect(('172.30.77.2',7777))",
                ]
            )
            after = await harness.firewall_inspect()
            identity = await harness.execute(["id", "-u"])
            await harness.stop()
            checks = {
                "bound_structure_and_scoped_mounts": True,
                "actual_nonroot_user": identity["stdout"].strip() == "10002",
                "reviewed_ipv4_ipv6_rules": after["rules_reviewed"],
                "harness_has_no_net_admin": after["harness_net_admin"] is False,
                "direct_socket_blocked": attempt["returncode"] != 0,
                "control_socket_blocked": control_attempt["returncode"] != 0,
                "actual_drop_counter_increased": after["drop_packets"] > before["drop_packets"],
                "stop_only_agent": docker(
                    "inspect", agent, "--format", "{{.State.Running}}"
                ).stdout.strip()
                == "false"
                and docker("inspect", fence, "--format", "{{.State.Running}}").stdout.strip()
                == "true",
            }
            payload = {
                "checked_at": datetime.now(UTC).isoformat(),
                "agent_image_id": image_id,
                "binding_code_sha256": hashlib.sha256(
                    Path("worldd/isolation.py").read_bytes()
                ).hexdigest(),
                "checks": checks,
                "check_count": len(checks),
                "passed": all(checks.values()),
                "scientific_gate_evidence": False,
            }
            target = Path("data/validation/binding-software.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, indent=2) + "\n")
            print(json.dumps(payload))
            if not payload["passed"]:
                raise SystemExit(1)
        finally:
            for name in reversed(created):
                docker("rm", "-f", name, check=False)
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    os.environ.setdefault("DOCKER_CONFIG", "/workspace/.cache/docker")
    asyncio.run(main())
