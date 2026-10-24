import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from worldd.harness_config import BUILTINS, HarnessManifest, binary_path, launch_configuration
from worldd.harnesses import MAX_LOG, HarnessService, _birth
from worldd.security import Capabilities
from worldd.storage import Store


class Auth:
    async def status(self, harness_id, instance_id="default"):
        return {"state": "connected", "auth_method": "subscription"}

    def profile_path(self, harness_id, instance_id="default"):
        return Path("/private/official-profile")

    async def aclose(self):
        pass


@pytest.fixture
async def native(tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text("""import json,os,sys,time
if sys.argv[1]=='version':print('fixture 1.2.3');raise SystemExit(0)
prompt=sys.stdin.read()
print(json.dumps({'type':'assistant','message':{'id':'msg_real_fixture','content':prompt[:20]},'WORLD_RUN_TOKEN':os.environ.get('WORLD_RUN_TOKEN'),'forbidden_present':any(k in os.environ for k in ('WORLD_ANTHROPIC_API_KEY','WORLD_CONTROL_TOKEN','AWS_SECRET_ACCESS_KEY'))}),flush=True)
print('access_token="never-return-this-credential"',file=sys.stderr,flush=True)
if sys.argv[1]=='fail':raise SystemExit(7)
if sys.argv[1]=='flood':print('x'*3000000,flush=True)
if sys.argv[1]=='hang':
 for i in range(200):print(json.dumps({'tick':i}),flush=True);time.sleep(.05)
""")
    store = Store(tmp_path / "data")
    capabilities = Capabilities(store.data_dir)
    world = SimpleNamespace(store=store, capabilities=capabilities, services={})
    mode = ["success"]

    def launcher(**options):
        return (
            [sys.executable, str(script), mode[0]],
            options["environment"],
            b"Actual World briefing",
        )

    definition = HarnessManifest(
        "fixture",
        "Real subprocess fixture",
        sys.executable,
        version_args=(str(script), "version"),
        launcher=launcher,
    )
    service = HarnessService(
        world,
        registry=[definition],
        auth=Auth(),
        environment={
            "PATH": os.environ["PATH"],
            "WORLD_ANTHROPIC_API_KEY": "world-provider-secret",
            "WORLD_CONTROL_TOKEN": "world-control-secret",
            "AWS_SECRET_ACCESS_KEY": "ambient-never-forward",
        },
    )
    world.services["harnesses"] = service
    world.run_config = lambda run_id: store.get_run(run_id)["config"]

    async def end_run(run_id, **kwargs):
        await service.stop_for_run(run_id)
        if store.get_run(run_id)["state"] != "sealed":
            store.append_record(run_id, "seal", **kwargs)
            store.update_run(run_id, state="sealed")

    world.end_run = end_run

    def create(**changes):
        config = {
            "budget": {"usd": 1, "wall_s": 60},
            "isolation_profile": "cooperative",
            "research_mode": "development",
            "capture_model_io": True,
            "model_snapshot_id": "fixture-model",
            "model_billing_mode": "api_key",
            **changes,
        }
        info = store.create_run(config)
        (store.run_dir(info["run_id"]) / "work").mkdir()
        return {
            **info,
            "preamble": "Actual World briefing",
            "run_token": capabilities.issue(info["run_id"]),
        }

    yield service, world, create, mode, script
    await service.aclose()
    capabilities.close()
    store.close()


async def test_real_process_exit_seals_run_and_redacts_bounded_stable_logs(native):
    service, world, create, _mode, _script = native
    info = create()
    session = await service.launch(info, "fixture", model="fixture-model")
    assert session["state"] == "running" and "pid" not in session
    await asyncio.wait_for(service.tasks[session["session_id"]], 5)
    result = service.get(session["session_id"])
    assert result["state"] == "completed" and result["exit_code"] == 0
    assert world.store.get_run(info["run_id"])["state"] == "sealed"
    assert world.store.records(info["run_id"])[-2]["kind"] == "harness_exit"
    assert world.store.verify_record(info["run_id"])["valid"]
    logs = service.logs(session["session_id"])
    text = json.dumps(logs)
    assert info["run_token"] not in text and "never-return-this-credential" not in text
    assert '"forbidden_present": false' in "\n".join(row["text"] for row in logs["events"])
    assert {row["channel"] for row in logs["events"]} == {"stdout", "stderr"}
    assert not service.logs(session["session_id"], logs["cursor"])["events"]


async def test_stop_paused_process_group_and_failure_are_actual(native):
    service, world, create, mode, _script = native
    mode[0] = "hang"
    info = create()
    session = await service.launch(info, "fixture", model="fixture-model")
    session_id = session["session_id"]
    await asyncio.sleep(0.2)
    world.store.update_run(info["run_id"], state="frozen")
    await asyncio.sleep(0.4)
    frozen = service.logs(session_id)["cursor"]
    await asyncio.sleep(0.2)
    assert service.logs(session_id)["cursor"] == frozen
    stopped = await service.stop(session_id)
    assert stopped["state"] == "stopped" and stopped["exit_code"] != 0
    assert not service.is_busy("fixture")
    mode[0] = "fail"
    info = create()
    failed = await service.launch(info, "fixture", model="fixture-model")
    await asyncio.wait_for(service.tasks[failed["session_id"]], 5)
    assert service.get(failed["session_id"])["exit_code"] == 7
    assert service.get(failed["session_id"])["state"] == "failed"


async def test_capture_caps_suppression_and_subscription_research_guard(native):
    service, world, create, mode, _script = native
    mode[0] = "flood"
    info = create()
    session = await service.launch(info, "fixture", model="fixture-model")
    await asyncio.wait_for(service.tasks[session["session_id"]], 5)
    assert service.get(session["session_id"])["output_truncated"]
    assert all(
        path.stat().st_size <= MAX_LOG
        for path in (service.root / session["session_id"]).glob("*.jsonl")
    )
    info = create(capture_model_io=False)
    suppressed = await service.launch(info, "fixture", model="fixture-model")
    await asyncio.wait_for(service.tasks[suppressed["session_id"]], 5)
    assert service.logs(suppressed["session_id"])["suppressed"]
    assert not service.logs(suppressed["session_id"])["events"]
    info = create(research_mode="research", model_billing_mode="subscription")
    with pytest.raises(ValueError, match="not measured"):
        await service.launch(info, "fixture", auth_mode="subscription")
    mode[0] = "success"
    info = create(stage=1, model_billing_mode="subscription")
    sub = await service.launch(info, "fixture", auth_mode="subscription")
    assert sub["billing"] == {"model": "unmeasured", "dollar_cap_scope": "world_tools_only"}
    await asyncio.wait_for(service.tasks[sub["session_id"]], 5)
    assert world.store.get_run(info["run_id"])["tainted"]


async def test_crash_recovery_checks_process_birth_and_never_invents_exit_status(native):
    service, world, create, _mode, _script = native
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(30)", start_new_session=True
    )
    info = create()
    value = {
        "session_id": "hs_recovery",
        "run_id": info["run_id"],
        "harness_id": "fixture",
        "instance_id": "default",
        "state": "running",
        "pid": process.pid,
        "birth": _birth(process.pid),
    }
    service._save(value)
    try:
        await service.recover()
        await asyncio.wait_for(process.wait(), 3)
        result = service.get("hs_recovery")
        assert result["state"] == "interrupted" and result["exit_code"] is None
        assert world.store.get_run(info["run_id"])["state"] == "sealed"
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def test_rejects_arbitrary_identifiers_model_flags_and_reused_run(native):
    service, _world, create, mode, _script = native
    with pytest.raises(ValueError):
        await service.install("fixture; echo unsafe")
    info = create()
    with pytest.raises(ValueError):
        await service.launch(info, "fixture", model="--model-provider=evil")
    with pytest.raises(ValueError):
        await service.launch(info, "fixture", model="fixture-model", instance_id="../other")
    mode[0] = "hang"
    session = await service.launch(info, "fixture", model="fixture-model")
    with pytest.raises(ValueError, match="already"):
        await service.launch(info, "fixture", model="fixture-model")
    await service.stop(session["session_id"])


@pytest.mark.parametrize("harness_id", ["claude-code", "codex"])
@pytest.mark.parametrize("auth_mode", ["api_key", "subscription"])
def test_builtin_binding_keeps_credentials_runtime_only(tmp_path, harness_id, auth_mode):
    root = tmp_path / "binding"
    root.mkdir()
    definition = next(item for item in BUILTINS if item.id == harness_id)
    argv, env, prompt = launch_configuration(
        definition,
        "/trusted/native-cli",
        root=root,
        work=tmp_path,
        run_info={
            "run_id": "r_fixture",
            "run_token": "never-persist-this-capability",
            "preamble": "Actual World briefing",
        },
        model="known-model",
        auth_mode=auth_mode,
        profile=tmp_path / "official-profile",
        agent_url="http://127.0.0.1:7777/agent",
        lane_url="http://127.0.0.1:7777/model",
        environment={
            "PATH": os.environ["PATH"],
            "WORLD_ANTHROPIC_API_KEY": "private-key",
            "WORLD_CONTROL_TOKEN": "private-control",
        },
    )
    assert argv[0] == "/trusted/native-cli" and prompt.startswith(b"Actual World briefing")
    assert b"Native harness layout:" in prompt
    assert "WORLD_CONTROL_TOKEN" not in env and "WORLD_ANTHROPIC_API_KEY" not in env
    assert env["WORLD_RUN_TOKEN"] == "never-persist-this-capability"
    assert "never-persist-this-capability" not in json.dumps(argv)
    assert all(
        "never-persist-this-capability" not in path.read_text() for path in root.glob("*.json")
    )
    if harness_id == "codex":
        assert 'web_search="disabled"' in argv and "--no-daemon" in argv
    elif auth_mode == "api_key":
        assert "X-World-Run-Token" in env["ANTHROPIC_CUSTOM_HEADERS"]
    else:
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env and "ANTHROPIC_BASE_URL" not in env


@pytest.mark.parametrize("stream_usage", [False, True])
def test_an_estimated_claude_session_streams_each_messages_final_usage(tmp_path, stream_usage):
    root = tmp_path / "binding"
    root.mkdir()
    definition = next(item for item in BUILTINS if item.id == "claude-code")
    argv, _env, _prompt = launch_configuration(
        definition,
        "/trusted/native-cli",
        root=root,
        work=tmp_path,
        run_info={"run_id": "r_fixture", "run_token": "capability", "preamble": "Briefing"},
        model="known-model",
        auth_mode="subscription",
        profile=tmp_path / "official-profile",
        agent_url="http://127.0.0.1:7777/agent",
        lane_url="http://127.0.0.1:7777/model",
        environment={"PATH": os.environ["PATH"]},
        stream_usage=stream_usage,
    )
    assert ("--include-partial-messages" in argv) is stream_usage
    assert argv[argv.index("--output-format") + 1] == "stream-json"


@pytest.mark.parametrize(
    ("harness_id", "expected"),
    [
        ("claude-code", ["--effort", "high"]),
        ("codex", ["-c", 'model_reasoning_effort="high"']),
    ],
)
def test_effort_reaches_each_client_in_its_own_form(tmp_path, harness_id, expected):
    root = tmp_path / "binding"
    root.mkdir()
    definition = next(item for item in BUILTINS if item.id == harness_id)
    options = {
        "root": root,
        "work": tmp_path,
        "run_info": {"run_id": "r_fixture", "run_token": "capability", "preamble": "Briefing"},
        "model": "known-model",
        "auth_mode": "subscription",
        "profile": tmp_path / "official-profile",
        "agent_url": "http://127.0.0.1:7777/agent",
        "lane_url": "http://127.0.0.1:7777/model",
        "environment": {"PATH": os.environ["PATH"]},
    }
    argv, _env, _prompt = launch_configuration(
        definition, "/trusted/native-cli", effort="high", **options
    )
    position = argv.index(expected[1])
    assert argv[position - 1] == expected[0]
    with pytest.raises(ValueError, match="effort"):
        launch_configuration(definition, "/trusted/native-cli", effort="extreme", **options)


async def test_a_run_launches_only_with_an_effort_its_harness_takes(native):
    service, _world, create, _mode, _script = native
    info = create(effort="high")
    with pytest.raises(ValueError, match="effort"):
        await service.launch(info, "fixture", model="fixture-model")


async def test_explicit_pinned_installer_is_not_triggered_by_discovery(native, tmp_path):
    service, _world, _create, _mode, _script = native
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invoked = tmp_path / "npm-was-invoked"
    npm = bin_dir / "npm"
    npm.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        f"pathlib.Path({str(invoked)!r}).write_text('called')\n"
        "args=sys.argv[1:]\n"
        "assert args[0]=='install' and args[-1]=='@fixed/fixture@1.2.3'\n"
        "assert '--ignore-scripts' in args and args[args.index('--registry')+1]=='https://registry.npmjs.org'\n"
        "prefix=pathlib.Path(args[args.index('--prefix')+1]);binary=prefix/'node_modules/.bin/fixture-cli'\n"
        "binary.parent.mkdir(parents=True);binary.write_text('#!/usr/bin/env python3\\nprint(\"fixture 1.2.3\")\\n');binary.chmod(0o700)\n"
    )
    npm.chmod(0o700)
    (bin_dir / "node").symlink_to(sys.executable)
    service.registry = {
        "fixture": HarnessManifest(
            "fixture",
            "Pinned fixture",
            "fixture-cli",
            package="@fixed/fixture",
            pinned_version="1.2.3",
            auth_modes=("api_key",),
        )
    }
    service.environment["PATH"] = (
        str(bin_dir) + os.pathsep + str(Path(sys.executable).parent) + ":/usr/bin:/bin"
    )
    library = await service.library()
    assert not library["items"][0]["installed"] and not invoked.exists()
    started = await service.install("fixture")
    assert started["state"] == "installing"
    await asyncio.wait_for(service.install_tasks["fixture"], 5)
    library = await service.library()
    assert invoked.exists() and library["items"][0]["installed"]
    assert library["items"][0]["version"] == "1.2.3"
    assert library["items"][0]["install_status"]["state"] == "installed"


async def test_installer_reports_missing_node_npm_without_any_spawn(native):
    service, _world, _create, _mode, _script = native
    service.registry = {
        "fixture": HarnessManifest(
            "fixture",
            "Pinned fixture",
            "fixture-cli",
            package="@fixed/fixture",
            pinned_version="1.2.3",
        )
    }
    service.environment["PATH"] = "/nonexistent"
    result = await service.install("fixture")
    assert result["state"] == "failed" and result["error"] == "node_npm_required"
    assert not service.install_tasks


async def test_unsupported_existing_cli_is_discovered_but_cannot_launch(native):
    service, _world, create, _mode, _script = native
    original = service.registry["fixture"]
    service.registry["fixture"] = HarnessManifest(
        "fixture",
        "Unsupported fixture",
        original.executable,
        pinned_version="9.9.9",
        version_args=original.version_args,
        launcher=original.launcher,
        auth_modes=("api_key",),
    )
    item = (await service.library())["items"][0]
    assert item["version"] == "1.2.3" and not item["installed"]
    assert not item["version_supported"]
    with pytest.raises(ValueError, match="supported pinned"):
        await service.launch(create(), "fixture", model="fixture-model")
    assert not service.processes


@pytest.mark.parametrize("failure", ["home_symlink", "config_symlink", "home_file"])
async def test_installer_setup_failure_finishes_without_spawn(
    native, tmp_path, monkeypatch, failure
):
    service, _world, _create, _mode, _script = native
    service.registry = {"claude-code": BUILTINS[0]}
    monkeypatch.setattr("worldd.harnesses.shutil.which", lambda *args, **kwargs: sys.executable)
    home = service.root / "installer-home"
    if failure == "home_symlink":
        home.symlink_to(tmp_path, target_is_directory=True)
    elif failure == "home_file":
        home.write_text("not a directory")
    else:
        home.mkdir()
        (home / "user.npmrc").symlink_to(tmp_path / "untouched")

    async def forbidden(*args, **kwargs):
        pytest.fail("Unsafe installer setup must never start npm")

    monkeypatch.setattr(service, "_bounded_command", forbidden)
    await service.install("claude-code")
    await service.install_tasks["claude-code"]
    saved = json.loads(service.db.execute("SELECT payload FROM native_installs").fetchone()[0])
    assert saved["state"] == "failed" and saved["finished_at"]
    assert not (tmp_path / "untouched").exists()


def test_claude_scriptless_native_package_requires_exact_manifest_and_containment(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("worldd.harness_config.platform.system", lambda: "Linux")
    monkeypatch.setattr("worldd.harness_config.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("worldd.harness_config.platform.libc_ver", lambda: ("glibc", "2.36"))
    manifest = BUILTINS[0]
    modules = tmp_path / "claude-code/node_modules"
    wrapper = modules / "@anthropic-ai/claude-code/package.json"
    package = modules / "@anthropic-ai/claude-code-linux-x64"
    package.mkdir(parents=True)
    wrapper.parent.mkdir()
    name = "@anthropic-ai/claude-code-linux-x64"
    wrapper.write_text(
        json.dumps(
            {
                "name": manifest.package,
                "version": "2.1.308",
                "optionalDependencies": {name: "2.1.308"},
            }
        )
    )
    metadata = package / "package.json"
    metadata.write_text(json.dumps({"name": name, "version": "2.1.308"}))
    binary = package / "claude"
    binary.write_text("native fixture")
    binary.chmod(0o700)
    assert binary_path(manifest, tmp_path, {"PATH": "/nonexistent"}) == str(binary)
    metadata.write_text(json.dumps({"name": name, "version": "wrong"}))
    assert binary_path(manifest, tmp_path, {"PATH": "/nonexistent"}) is None
    metadata.write_text(json.dumps({"name": name, "version": "2.1.308"}))
    binary.unlink()
    binary.symlink_to(sys.executable)
    assert binary_path(manifest, tmp_path, {"PATH": "/nonexistent"}) is None


async def test_native_output_bound_requires_actual_owned_codex_api_process(native):
    service, _world, create, mode, _script = native
    original = service.registry["fixture"]
    service.registry["codex"] = HarnessManifest(
        "codex",
        "Owned Codex fixture",
        original.executable,
        version_args=original.version_args,
        launcher=original.launcher,
        auth_modes=("api_key",),
    )
    info = create(
        native_output_token_limit=4096,
        model_pricing={"models": {"fixture-model": {"max_output_tokens": 2048}}},
    )
    assert service.output_limit(info["run_id"], "fixture-model") is None
    mode[0] = "hang"
    session = await service.launch(info, "codex", model="fixture-model")
    assert service.output_limit(info["run_id"], "fixture-model") == 2048
    assert service.output_limit(info["run_id"], "different-model") is None
    value = service._load(session["session_id"])
    value["birth"] = "not-the-actual-process"
    service._save(value)
    assert service.output_limit(info["run_id"], "fixture-model") is None
    await service.stop(session["session_id"])
    assert service.output_limit(info["run_id"], "fixture-model") is None
