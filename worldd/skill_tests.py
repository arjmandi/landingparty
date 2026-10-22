"""World-owned procedural skill checks in an explicitly configured Docker worker.

Only Docker's trusted CLI executes on the host. Candidate Python is copied into
an isolated, read-only container; it is never evaluated by the world process.
The production compose stack deliberately has no Docker socket or worker.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .util import blocked, identifier, now


def code_hash(value):
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillLimits:
    wall_seconds: float = 10
    output_bytes: int = 65536
    memory_bytes: int = 134217728
    nano_cpus: int = 500000000
    pids: int = 32
    tmp_bytes: int = 16777216

    def __post_init__(self):
        if (
            not 0.1 <= self.wall_seconds <= 60
            or not 1024 <= self.output_bytes <= 1048576
            or not 33554432 <= self.memory_bytes <= 536870912
            or not 100000000 <= self.nano_cpus <= 1000000000
            or not 4 <= self.pids <= 64
            or not 1048576 <= self.tmp_bytes <= 67108864
        ):
            raise ValueError("Skill worker limits must remain inside their bounded policy")


@dataclass(frozen=True)
class SkillExecution:
    image_id: str
    source_hash: str
    tests_hash: str
    passed: bool
    tests_executed: int | None
    exit_code: int | None
    reason: str
    stdout: str
    stderr: str
    limits: dict
    isolation_verified: bool


class SkillWorkerError(RuntimeError):
    pass


class OutputLimit(SkillWorkerError):
    pass


class CleanupUnconfirmed(SkillWorkerError):
    pass


# unittest counts executions rather than estimating from source syntax. A zero
# test suite, SystemExit, abrupt os._exit, malformed receipt, and any unittest
# failure cannot produce a passing receipt. Arbitrary Python tests can still be
# vacuous or dishonest; this is an execution check, never semantic proof.
_RUNNER = """import importlib.util
import json
import sys
import unittest

sys.path.insert(0, "/input")
receipt = {"passed": False, "tests_executed": 0}
try:
    specification = importlib.util.spec_from_file_location("skill_checks", "/input/test_skill.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    receipt = {"passed": result.wasSuccessful() and result.testsRun > 0,
               "tests_executed": result.testsRun}
except BaseException:
    import traceback
    traceback.print_exc(file=sys.stderr)
print("__WORLD_SKILL_RESULT__" + json.dumps({"nonce": NONCE, **receipt}), flush=True)
raise SystemExit(0 if receipt["passed"] else 1)
"""


class DockerSkillWorker:
    """Trusted host worker; it never pulls images or mounts the world workspace."""

    def __init__(self, image, *, limits=None, docker_binary=None, namespace="operator-cli"):
        if not isinstance(image, str) or not image or len(image) > 256 or image.startswith("-"):
            raise ValueError("A trusted skill-test image must be configured")
        self.image = image
        self.limits = limits or SkillLimits()
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self.containers = set()
        self.binary = docker_binary or shutil.which("docker")
        if self.binary and not Path(self.binary).is_absolute():
            raise ValueError("Docker worker executable must be an absolute trusted path")

    @staticmethod
    def client_environment():
        allowed = {
            "PATH",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "XDG_RUNTIME_DIR",
            "SSH_AUTH_SOCK",
        }
        return {key: value for key, value in os.environ.items() if key in allowed}

    async def _command(self, arguments, *, timeout=5, limit=65536):
        if not self.binary:
            raise SkillWorkerError("The trusted Docker worker is unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.client_environment(),
            )
        except OSError:
            raise SkillWorkerError("The trusted Docker worker could not start") from None

        size = 0

        async def read(stream):
            nonlocal size
            chunks = []
            while chunk := await stream.read(4096):
                size += len(chunk)
                if size > limit:
                    raise OutputLimit("The skill worker output limit was exceeded")
                chunks.append(chunk)
            return b"".join(chunks)

        readers = [
            asyncio.create_task(read(process.stdout)),
            asyncio.create_task(read(process.stderr)),
        ]
        try:
            output = await asyncio.wait_for(asyncio.gather(*readers), timeout)
            await asyncio.wait_for(process.wait(), 1)
            return process.returncode, *(part.decode("utf-8", errors="replace") for part in output)
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 1)

    async def _inspect_image(self):
        status, stdout, _ = await self._command(["image", "inspect", self.image])
        if status:
            raise SkillWorkerError("The configured image must already exist on the trusted worker")
        try:
            inspection = json.loads(stdout)[0]
            image_id = inspection["Id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ValueError
            if inspection.get("Config", {}).get("Volumes"):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise SkillWorkerError(
                "The worker image has invalid identity or implicit volumes"
            ) from None
        return image_id

    def command(self, name, image_id, directory):
        limits = self.limits
        return [
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            "landing-party.skill-worker=" + self.namespace,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            str(limits.nano_cpus / 1e9),
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "fsize=1048576:1048576",
            "--ipc",
            "none",
            "--log-driver",
            "none",
            "--stop-timeout",
            "1",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_bytes},mode=1777",
            "--mount",
            f"type=bind,source={directory},target=/input,readonly",
            "--workdir",
            "/input",
            "--entrypoint",
            "/usr/bin/env",
            image_id,
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/tmp",
            "LANG=C.UTF-8",
            "/usr/local/bin/python3",
            "-I",
            "-B",
            "/input/_world_runner.py",
        ]

    async def _verify_container(self, name, image_id, directory):
        status, stdout, _ = await self._command(["container", "inspect", name])
        try:
            item = json.loads(stdout)[0]
            host, config = item["HostConfig"], item["Config"]
            mounts = item["Mounts"]
            if (
                status
                or item["Image"] != image_id
                or config["User"] != "65534:65534"
                or host["NetworkMode"] != "none"
                or host["ReadonlyRootfs"] is not True
                or host["Privileged"]
                or host["PidMode"] == "host"
                or host["IpcMode"] != "none"
                or host["CapAdd"]
                or "ALL" not in host["CapDrop"]
                or "no-new-privileges" not in host["SecurityOpt"]
                or host["PidsLimit"] != self.limits.pids
                or host["Memory"] != self.limits.memory_bytes
                or host["MemorySwap"] != self.limits.memory_bytes
                or host["NanoCpus"] != self.limits.nano_cpus
                or host.get("Devices")
                or host.get("VolumesFrom")
                or host.get("Binds")
                or len(mounts) != 1
                or mounts[0]["Type"] != "bind"
                or mounts[0]["RW"]
                or mounts[0]["Source"] != str(directory)
                or mounts[0]["Destination"] != "/input"
            ):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise SkillWorkerError(
                "Docker did not apply the required skill-test isolation"
            ) from None

    @staticmethod
    def _result(stdout, nonce):
        candidates = [
            line[len("__WORLD_SKILL_RESULT__") :]
            for line in stdout.splitlines()
            if line.startswith("__WORLD_SKILL_RESULT__")
        ]
        if len(candidates) != 1:
            return None
        try:
            result = json.loads(candidates[0])
            if (
                set(result) != {"nonce", "passed", "tests_executed"}
                or result["nonce"] != nonce
                or type(result["passed"]) is not bool
                or type(result["tests_executed"]) is not int
                or not 0 <= result["tests_executed"] <= 100000
            ):
                return None
            return result
        except (ValueError, KeyError, TypeError):
            return None

    async def execute(self, source, tests):
        if (
            not isinstance(source, str)
            or not isinstance(tests, str)
            or len((source + tests).encode("utf-8")) > 500000
        ):
            raise SkillWorkerError("Skill worker input must be bounded UTF-8 source")
        image_id = await self._inspect_image()
        name = "landing-party-skill-" + secrets.token_hex(12)
        nonce = secrets.token_hex(32)
        stdout, stderr, exit_code, reason, result = "", "", None, "worker_error", None
        created = False
        self.containers.add(name)
        with tempfile.TemporaryDirectory(prefix="landing-party-skill-") as temporary:
            directory = Path(temporary).resolve()
            (directory / "skill.py").write_text(source, encoding="utf-8")
            (directory / "test_skill.py").write_text(tests, encoding="utf-8")
            (directory / "_world_runner.py").write_text(
                "NONCE = " + repr(nonce) + "\n" + _RUNNER, encoding="utf-8"
            )
            for path in directory.iterdir():
                path.chmod(0o444)
            directory.chmod(0o555)
            try:
                code, _, _ = await self._command(self.command(name, image_id, directory))
                if code:
                    raise SkillWorkerError("Docker could not create the isolated skill container")
                created = True
                await self._verify_container(name, image_id, directory)
                try:
                    exit_code, stdout, stderr = await self._command(
                        ["start", "--attach", name],
                        timeout=self.limits.wall_seconds,
                        limit=self.limits.output_bytes,
                    )
                    result = self._result(stdout, nonce)
                    reason = "completed" if result else "missing_execution_receipt"
                except TimeoutError:
                    reason = "wall_limit"
                except OutputLimit:
                    reason = "output_limit"
            finally:
                try:
                    code, _, _ = await self._command(["rm", "--force", name], timeout=2)
                    if code and created:
                        raise CleanupUnconfirmed(
                            "The skill container cleanup could not be confirmed"
                        )
                    self.containers.discard(name)
                finally:
                    directory.chmod(0o700)
        return SkillExecution(
            image_id=image_id,
            source_hash=code_hash(source),
            tests_hash=code_hash(tests),
            passed=bool(
                result and result["passed"] and result["tests_executed"] > 0 and exit_code == 0
            ),
            tests_executed=result["tests_executed"] if result else None,
            exit_code=exit_code,
            reason=reason,
            stdout=stdout,
            stderr=stderr,
            limits=asdict(self.limits),
            isolation_verified=True,
        )

    async def recover(self):
        """Remove only interrupted containers bearing this world's worker label."""
        code, stdout, _ = await self._command(
            [
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=landing-party.skill-worker=" + self.namespace,
            ]
        )
        names = stdout.split()
        if code or any(not re.fullmatch(r"[0-9a-f]{64}", name) for name in names):
            raise SkillWorkerError("The worker cannot verify its interrupted containers")
        if names:
            code, _, _ = await self._command(["rm", "--force", *names])
            if code:
                raise SkillWorkerError("The worker cannot remove its interrupted containers")


class SkillTestService:
    """Researcher-only test execution; no request can supply passing metadata."""

    def __init__(self, world, *, worker=None, image=None, max_concurrent=2):
        self.world, self.store = world, world.store
        selected = image or os.getenv("WORLD_SKILL_TEST_IMAGE")
        self.worker = worker
        if worker is None and selected and os.getenv("WORLD_SKILL_TEST_WORKER") == "local-docker":
            self.worker = DockerSkillWorker(selected, namespace=str(self.store.data_dir.resolve()))
        self.max_concurrent = max_concurrent
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 8:
            raise ValueError("Skill worker concurrency must be between one and eight")
        self.available = worker is not None or self.worker is not None
        self.active = {}
        self.closed = False

    async def run(self, run_id, name, source, tests, *, promote=False):
        if type(promote) is not bool:
            raise ValueError("promote must be a boolean")
        run = self.store.get_run(run_id)
        if self.closed or run["state"] != "running":
            return blocked(
                "policy_deny", "Skill checks require a running world session", "run_state"
            )
        if run["config"].get("stage", 0) < 1 or "belief" not in run["config"].get("packs", []):
            return blocked("policy_deny", "Skill checks require the belief pack", "skill_stage")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", name):
            raise ValueError("Invalid skill name")
        if (
            not isinstance(source, str)
            or not isinstance(tests, str)
            or len((source + tests).encode("utf-8")) > 500000
        ):
            raise ValueError("Skill code and unittest source must be bounded text")
        if self.worker is None or not self.available:
            return blocked(
                "policy_deny", "No trusted isolated skill worker is configured", "skill_worker"
            )
        if hasattr(self.world, "admission_guard") and (
            denial := self.world.admission_guard(run_id)
        ):
            return denial
        if sum(len(tasks) for tasks in self.active.values()) >= self.max_concurrent:
            return blocked(
                "too_many_in_flight", "The skill worker is at capacity", "skill_concurrency"
            )
        task = asyncio.current_task()
        self.active.setdefault(run_id, set()).add(task)
        job_id = identifier("skilltest")
        base = {
            "job_id": job_id,
            "name": name,
            "source_hash": code_hash(source),
            "tests_hash": code_hash(tests),
        }
        self.store.append_record(run_id, "skill_test_started", **base)
        try:
            execution = await self.worker.execute(source, tests)
            if (
                not isinstance(execution, SkillExecution)
                or not execution.isolation_verified
                or execution.source_hash != base["source_hash"]
                or execution.tests_hash != base["tests_hash"]
                or type(execution.passed) is not bool
                or (
                    execution.tests_executed is not None
                    and type(execution.tests_executed) is not int
                )
                or (
                    execution.passed
                    and (
                        not execution.tests_executed
                        or execution.tests_executed < 0
                        or execution.exit_code != 0
                    )
                )
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", execution.image_id)
            ):
                raise SkillWorkerError("The worker returned invalid execution evidence")
            logs = self.store.data_dir / "control" / "skill-tests"
            logs.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = logs / (job_id + ".json")
            target.write_text(json.dumps(asdict(execution)), encoding="utf-8")
            target.chmod(0o600)
            receipt = self.store.append_record(
                run_id,
                "skill_test",
                **base,
                event_id=identifier("e"),
                passed=execution.passed,
                tests_executed=execution.tests_executed,
                image_id=execution.image_id,
                isolation={
                    **execution.limits,
                    "network": "none",
                    "read_only": True,
                    "capabilities": [],
                    "no_new_privileges": True,
                    "user": "65534:65534",
                    "input_read_only": True,
                    "private_mounts": False,
                    "provider_environment": False,
                },
                isolation_verified=True,
                exit_code=execution.exit_code,
                reason=execution.reason,
                stdout_hash=code_hash(execution.stdout),
                stderr_hash=code_hash(execution.stderr),
                finished_at=now(),
                executor="docker-unittest@1",
            )
            response = {"kind": "result", "receipt": receipt, "passed": execution.passed}
            if promote and execution.passed:
                response["skill"] = self.world.services["beliefs"].promote_skill(
                    run_id, name, source, tests, receipt["event_id"]
                )
            return response
        except asyncio.CancelledError:
            self.store.append_record(run_id, "skill_test_cancelled", **base)
            return blocked(
                "policy_deny", "The isolated skill check was cancelled", "skill_cancelled"
            )
        except (SkillWorkerError, OSError, TimeoutError) as error:
            if isinstance(error, CleanupUnconfirmed):
                self.store.update_run(run_id, tainted=True)
                self.available = False
            self.store.append_record(run_id, "skill_test_failed", **base, reason="worker_boundary")
            return blocked(
                "policy_deny", "The isolated worker could not verify execution", "skill_worker"
            )
        finally:
            self.active[run_id].discard(task)
            if not self.active[run_id]:
                self.active.pop(run_id)

    async def stop_run(self, run_id):
        tasks = [
            task for task in self.active.get(run_id, set()) if task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recover(self):
        if self.worker and hasattr(self.worker, "recover"):
            try:
                await self.worker.recover()
                self.available = True
            except (SkillWorkerError, OSError, TimeoutError):
                self.available = False

    async def aclose(self):
        self.closed = True
        for run_id in list(self.active):
            await self.stop_run(run_id)


def main():
    parser = argparse.ArgumentParser(description="Trusted host-only isolated skill-test worker")
    parser.add_argument(
        "--image", required=True, help="Already installed trusted image, pinned after inspection"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--tests", required=True, type=Path, help="Python unittest module")
    args = parser.parse_args()

    async def execute():
        try:
            result = await DockerSkillWorker(args.image).execute(
                args.source.read_text(encoding="utf-8"), args.tests.read_text(encoding="utf-8")
            )
        except (SkillWorkerError, OSError, TimeoutError):
            print(json.dumps({"passed": False, "reason": "worker_unavailable"}))
            return 1
        print(json.dumps(asdict(result)))
        return 0 if result.passed else 1

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
