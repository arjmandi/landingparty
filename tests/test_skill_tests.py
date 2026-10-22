"""Skill execution receipts come from a configured isolated worker, never JSON flags."""

import asyncio
import json
import os

import pytest

from worldd.kernel import BeliefService
from worldd.models import RunConfig
from worldd.registry import RegistryService
from worldd.runtime import World
from worldd.skill_tests import (
    DockerSkillWorker,
    SkillLimits,
    SkillTestService,
    SkillWorkerError,
    code_hash,
)
from worldd.storage import Store

SOURCE = "def double(value):\n    return value * 2\n"
TESTS = """import unittest
from skill import double

class DoubleTests(unittest.TestCase):
    def test_double(self):
        self.assertEqual(double(4), 8)
"""
IMAGE = "sha256:9e87977b867847e186d066f531ef783b006d582a985c341c269446088d90f2c4"
docker = pytest.mark.skipif(
    os.getenv("WORLD_RUN_DOCKER_SKILL_TESTS") != "1",
    reason="Actual Docker skill checks are an explicit trusted-host smoke test",
)


class Gateway:
    def __init__(self):
        self.policy = {}

    async def aclose(self):
        pass


@pytest.fixture
async def world(tmp_path):
    result = World(Store(tmp_path), Gateway())
    result.services.update(registry=RegistryService(result), beliefs=BeliefService(result))
    yield result
    await result.aclose()


def skill_run(world):
    return world.create_run(RunConfig(stage=1, packs=["belief"]))["run_id"]


async def test_unconfigured_worker_cannot_execute_or_mint_promotion_receipt(world, monkeypatch):
    monkeypatch.delenv("WORLD_SKILL_TEST_WORKER", raising=False)
    monkeypatch.delenv("WORLD_SKILL_TEST_IMAGE", raising=False)
    run_id = skill_run(world)
    result = await SkillTestService(world).run(run_id, "double", SOURCE, TESTS, promote=True)
    assert result["kind"] == "blocked" and result["rule_id"] == "skill_worker"
    assert not any(row["kind"] == "skill_test" for row in world.store.records(run_id))
    assert world.services["beliefs"].skills(run_id) == []


async def test_worker_image_alone_does_not_enable_host_docker(world, monkeypatch):
    monkeypatch.delenv("WORLD_SKILL_TEST_WORKER", raising=False)
    service = SkillTestService(world, image=IMAGE)
    assert service.worker is None


async def test_passing_request_metadata_cannot_replace_execution_evidence(world):
    class InvalidWorker:
        async def execute(self, source, tests):
            return {"passed": True, "tests_executed": 999, "source_hash": code_hash(source)}

    run_id = skill_run(world)
    reply = await SkillTestService(world, worker=InvalidWorker()).run(
        run_id, "double", SOURCE, TESTS, promote=True
    )
    assert reply["kind"] == "blocked" and reply["rule_id"] == "skill_worker"
    assert not any(row["kind"] == "skill_test" for row in world.store.records(run_id))
    assert world.services["beliefs"].skills(run_id) == []


def test_container_policy_pins_image_and_mounts_only_immutable_inputs(tmp_path):
    worker = DockerSkillWorker(IMAGE, docker_binary="/trusted/docker")
    command = worker.command("fixture", IMAGE, tmp_path)
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "65534:65534"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert "--read-only" in command and "--pull=never" in command
    assert command.count("--mount") == 1
    assert (
        command[command.index("--mount") + 1]
        == f"type=bind,source={tmp_path},target=/input,readonly"
    )
    assert command[command.index("--entrypoint") + 2] == IMAGE
    assert command[command.index(IMAGE) + 1] == "-i"
    assert "--pids-limit" in command and "--memory" in command and "--cpus" in command
    assert not any("docker.sock" in value or "privileged" in value for value in command)


def test_host_worker_environment_excludes_world_and_provider_keys(monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "private-control")
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "private-provider")
    monkeypatch.setenv("DOCKER_CONFIG", "/operator/docker-config")
    environment = DockerSkillWorker.client_environment()
    assert "WORLD_CONTROL_TOKEN" not in environment and "WORLD_OPENAI_API_KEY" not in environment
    assert environment["DOCKER_CONFIG"] == "/operator/docker-config"


def test_absent_duplicate_or_invalid_unittest_receipts_fail_closed():
    nonce = "fixture_nonce"
    marker = "__WORLD_SKILL_RESULT__"
    result = marker + json.dumps({"nonce": nonce, "passed": True, "tests_executed": 1})
    assert DockerSkillWorker._result(result, nonce)["tests_executed"] == 1
    assert DockerSkillWorker._result('{"passed":true,"tests_executed":1}', nonce) is None
    assert DockerSkillWorker._result(result + "\n" + result, nonce) is None
    assert DockerSkillWorker._result(result, "different_nonce") is None
    assert (
        DockerSkillWorker._result(
            marker + json.dumps({"nonce": nonce, "passed": True, "tests_executed": True}), nonce
        )
        is None
    )


@docker
async def test_actual_docker_unittest_receipt_promotes_exact_code(world, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    worker = DockerSkillWorker(IMAGE)
    service = SkillTestService(world, worker=worker)
    world.services["skill_tests"] = service
    run_id = skill_run(world)
    reply = await service.run(run_id, "double", SOURCE, TESTS, promote=True)
    assert reply["kind"] == "result" and reply["passed"], reply
    receipt = reply["receipt"]
    assert receipt["tests_executed"] == 1
    assert receipt["source_hash"] == code_hash(SOURCE) and receipt["tests_hash"] == code_hash(TESTS)
    assert receipt["image_id"] == IMAGE and receipt["isolation_verified"]
    assert reply["skill"]["test_evidence_ref"] == receipt["event_id"]
    assert world.services["beliefs"].skills(run_id)[0]["source"] == SOURCE
    assert worker.containers == set()


@docker
async def test_actual_docker_has_no_credentials_network_or_writable_inputs(world, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "must-never-enter-container")
    tests = """import os, pathlib, socket, unittest
from skill import double

class IsolationTests(unittest.TestCase):
    def test_actual_function(self):
        self.assertEqual(double(6), 12)
    def test_no_secrets_or_daemon(self):
        self.assertFalse(any(name.startswith("WORLD_") for name in os.environ))
        self.assertFalse(pathlib.Path("/var/run/docker.sock").exists())
        self.assertNotEqual(os.getuid(), 0)
    def test_immutable_source_and_root(self):
        with self.assertRaises(OSError):
            pathlib.Path("/input/skill.py").write_text("tampered")
        with self.assertRaises(OSError):
            pathlib.Path("/etc/world-write").write_text("tampered")
    def test_network_denied(self):
        with self.assertRaises(OSError):
            socket.create_connection(("93.184.216.34", 443), timeout=0.1)
    def test_actual_resource_limits(self):
        status = pathlib.Path("/proc/self/status").read_text()
        self.assertIn("CapEff:\\t0000000000000000", status)
        self.assertIn("NoNewPrivs:\\t1", status)
        self.assertEqual(pathlib.Path("/sys/fs/cgroup/memory.max").read_text().strip(), "134217728")
        self.assertEqual(pathlib.Path("/sys/fs/cgroup/pids.max").read_text().strip(), "32")
        quota, period = map(int, pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().split())
        self.assertEqual(quota / period, 0.5)
"""
    result = await DockerSkillWorker(IMAGE).execute(SOURCE, tests)
    assert result.passed and result.tests_executed == 5, result


@docker
@pytest.mark.parametrize(
    "tests",
    [
        "import unittest\nclass Fail(unittest.TestCase):\n def test_fail(self):\n  self.fail('fixture failure')\n",
        "assert True\n",  # Executing a module is not an executed unittest.
        "import os\nos._exit(0)\n",  # A clean process exit is not a receipt.
    ],
)
async def test_actual_failure_empty_suite_and_abrupt_exit_never_pass(tests, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    execution = await DockerSkillWorker(IMAGE).execute(SOURCE, tests)
    assert not execution.passed


@docker
async def test_actual_output_and_wall_limits_remove_container(monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    for tests, reason in [
        ("print('x'*100000)\n", "output_limit"),
        ("import time\ntime.sleep(30)\n", "wall_limit"),
    ]:
        worker = DockerSkillWorker(
            IMAGE,
            limits=SkillLimits(
                wall_seconds=5 if reason == "output_limit" else 0.5, output_bytes=1024
            ),
        )
        result = await worker.execute(SOURCE, tests)
        assert not result.passed and result.reason == reason
        assert worker.containers == set()


@docker
async def test_end_run_cancels_actual_container_before_sealing(world, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    worker = DockerSkillWorker(IMAGE, limits=SkillLimits(wall_seconds=60))
    service = SkillTestService(world, worker=worker)
    world.services["skill_tests"] = service
    run_id = skill_run(world)
    pending = asyncio.create_task(
        service.run(run_id, "slow", SOURCE, "import time\ntime.sleep(59)\n")
    )
    running = False
    for _ in range(100):
        if worker.containers:
            code, output, _ = await worker._command(
                ["inspect", "--format", "{{.State.Running}}", next(iter(worker.containers))]
            )
            if code == 0 and output.strip() == "true":
                running = True
                break
        await asyncio.sleep(0.01)
    assert running, "The long-running candidate must actually start before cancellation"
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(world.end_run(run_id), 3)
    assert asyncio.get_running_loop().time() - started < 2
    result = await pending
    assert result["rule_id"] == "skill_cancelled"
    records = world.store.records(run_id)
    assert records[-1]["kind"] == "seal"
    assert (
        next(row["seq"] for row in records if row["kind"] == "skill_test_cancelled")
        < records[-1]["seq"]
    )
    assert worker.containers == set() and service.active == {}
    assert world.store.verify_record(run_id)["valid"]


async def test_unavailable_recovery_leaves_optional_worker_closed(world):
    class Unavailable:
        async def recover(self):
            raise SkillWorkerError("Docker is unavailable")

        async def execute(self, source, tests):
            pytest.fail("An unavailable worker must not execute candidate code")

    service = SkillTestService(world, worker=Unavailable())
    await service.recover()
    assert not service.available
    assert (await service.run(skill_run(world), "double", SOURCE, TESTS))["kind"] == "blocked"


@docker
async def test_recovery_removes_only_this_worlds_interrupted_containers(monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", "/workspace/.cache/docker")
    worker = DockerSkillWorker(IMAGE, namespace="recovery-fixture-" + os.urandom(8).hex())
    name = "landing-party-skill-recovery-" + os.urandom(8).hex()
    code, _, _ = await worker._command(
        [
            "create",
            "--name",
            name,
            "--label",
            "landing-party.skill-worker=" + worker.namespace,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--entrypoint",
            "/bin/true",
            IMAGE,
        ]
    )
    assert code == 0
    try:
        await worker.recover()
        code, _, _ = await worker._command(["inspect", name])
        assert code != 0
    finally:
        await worker._command(["rm", "--force", name])
