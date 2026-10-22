"""Explicit optional Harbor SDK check against disposable, actual fenced Docker containers.

Run: PYTHONPATH=. python tests/native_harbor_smoke.py --output /tmp/harbor-pilot
Requires the standards extra, Docker, and landing-party-agent:0.1. No paid calls.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from harbor.environments.factory import EnvironmentFactory
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task
from harbor.models.trial.paths import TrialPaths

from faces.harbor.package import build_task, write_job


def docker(*argv, check=True):
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=45
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr[:1000])
    return result


async def run(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    run_id = "r_harbor_" + uuid.uuid4().hex[:10]
    network, fence, agent = (run_id + suffix for suffix in ("-net", "-fence", "-agent"))
    created, environment = [], None
    data = output / "store"
    agent_logs = output / "trial" / "agent"
    agent_logs.mkdir(parents=True)
    agent_logs.chmod(0o777)
    for part in ("work", "world"):
        path = data / "runs" / run_id / part
        path.mkdir(parents=True)
        path.chmod(0o777 if part == "work" else 0o755)
    package = build_task(
        output / "task", instruction="Native Harbor software fixture.", run_id=run_id
    )
    task = Task(package)
    job = write_job(
        output / "job.json",
        task_dir=package,
        run_id=run_id,
        container_id=agent,
        firewall_id=fence,
        data_dir=data,
    )
    config = JobConfig.model_validate(job)
    checks = {"native_task_and_job_schema": True}
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
            if docker("exec", fence, "test", "-f", "/tmp/fence-ready", check=False).returncode == 0:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Fence initializer did not become ready")
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
            "--mount",
            f"type=bind,src={agent_logs},dst=/logs/agent",
            image_id,
            "sleep",
            "infinity",
        )
        created.append(agent)
        environment = EnvironmentFactory.create_environment_from_config(
            config.environment,
            environment_dir=package / "environment",
            environment_name="landing-party",
            session_id=run_id,
            trial_paths=TrialPaths(output / "trial"),
            task_env_config=task.config.environment,
        )
        await environment.start(force_build=False)
        checks["native_environment_factory_and_start"] = True
        result = await environment.exec("id -u", cwd="/work")
        checks["actual_unprivileged_exec"] = (
            result.return_code == 0 and result.stdout.strip() == "10002"
        )
        result = await environment.exec("printf native-log > /logs/agent/check.txt")
        checks["actual_harbor_log_mount"] = (
            result.return_code == 0 and (agent_logs / "check.txt").read_text() == "native-log"
        )
        source = output / "source"
        source.mkdir()
        (source / "nested").mkdir()
        content = b"Native transfer fixture\n" * 5000
        (source / "nested" / "binary.dat").write_bytes(content)
        await environment.upload_dir(source, "/work/uploaded")
        await environment.download_dir("/work/uploaded", output / "downloaded")
        checks["actual_chunked_directory_transfer"] = (
            output / "downloaded" / "nested" / "binary.dat"
        ).read_bytes() == content
        for label, callback in (
            ("root_exec_refused", lambda: environment.exec("id", user="root")),
            (
                "world_upload_refused",
                lambda: environment.upload_file(
                    source / "nested" / "binary.dat", "/world/forbidden"
                ),
            ),
            (
                "private_path_download_refused",
                lambda: environment.download_file("/etc/passwd", output / "forbidden"),
            ),
        ):
            try:
                await callback()
            except ValueError:
                checks[label] = True
            else:
                checks[label] = False
        before = await environment.harness.firewall_inspect()
        result = await environment.exec(
            "python3 -c \"import socket;s=socket.socket();s.settimeout(.5);s.connect(('172.30.77.2',7777))\""
        )
        after = await environment.harness.firewall_inspect()
        checks["actual_control_egress_drop"] = (
            result.return_code != 0 and after["drop_packets"] > before["drop_packets"]
        )
        checks["reviewed_rules_and_no_net_admin"] = (
            after["rules_reviewed"] and not after["harness_net_admin"]
        )
        await environment.stop(delete=False)
        checks["stop_only_bound_agent"] = (
            docker("inspect", agent, "--format", "{{.State.Running}}").stdout.strip() == "false"
            and docker("inspect", fence, "--format", "{{.State.Running}}").stdout.strip() == "true"
        )
        report = {
            "checked_at": datetime.now(UTC).isoformat(),
            "harbor_version": version("harbor"),
            "image_id": image_id,
            "adapter_hash": "sha256:"
            + hashlib.sha256(Path("faces/harbor/native.py").read_bytes()).hexdigest(),
            "checks": checks,
            "passed": all(checks.values()),
            "scientific_gate_evidence": False,
            "paid_external_calls": 0,
        }
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        assert report["passed"], checks
        return report
    finally:
        for name in reversed(created):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("DOCKER_CONFIG", "/workspace/.cache/docker")
    print(json.dumps(asyncio.run(run(args.output)), indent=2))
