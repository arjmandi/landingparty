"""Native CLI subprocess fixtures; no real account login or provider calls."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import pytest

from worldd.harness_auth import HarnessAuthService, _process_start, _safe_url

FIXTURE = r"""
import json,os,pathlib,subprocess,sys,time
if sys.argv[1:]==['--version']:
 print('native-fixture '+('0.163.0' if 'codex' in pathlib.Path(sys.argv[0]).name else '2.1.308'));sys.exit(0)
kind = 'claude-code' if 'CLAUDE_CONFIG_DIR' in os.environ else 'codex'
root = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or os.environ['CODEX_HOME'])
root.mkdir(parents=True,exist_ok=True)
scenario_path = root/'scenario.json'
scenario = json.loads(scenario_path.read_text()) if scenario_path.exists() else {}
with (root/'calls.jsonl').open('a') as stream:
 stream.write(json.dumps({'argv':sys.argv[1:],'profile':str(root),'home':os.environ['HOME'],
  'ambient_secrets':any(key in os.environ for key in ['OPENAI_API_KEY','ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','CODEX_ACCESS_TOKEN']),
  'tty':os.isatty(0)})+'\n')
state_path=root/'state.json'
state=json.loads(state_path.read_text()) if state_path.exists() else {}
if sys.argv[1:] in (['auth','status','--json'],['login','status']):
 if scenario.get('hang_status'):time.sleep(20)
 if scenario.get('invalid_status'):print('[]');sys.exit(0)
 if scenario.get('flood_status'):
  print('Logged in using ChatGPT');print('x'*100000);sys.exit(0)
 if kind=='claude-code':
  print(json.dumps({'loggedIn':state.get('connected',False),'authMethod':state.get('method','none'),
   'apiProvider':'firstParty','email':'private@example.org','access_token':'NEVER_PUBLIC_ACCESS','refresh_token':'NEVER_PUBLIC_REFRESH'}))
 else:
  if state.get('connected'):
   print('Logged in using ChatGPT' if state.get('method')=='subscription' else 'Logged in using an API key: sk-NEVER_PUBLIC_KEY',file=sys.stderr)
  else:print('Not logged in',file=sys.stderr)
 sys.exit(0 if state.get('connected') else 1)
if sys.argv[1:] in (['auth','logout'],['logout']):
 state_path.unlink(missing_ok=True);sys.exit(0)
expected=['auth','login','--claudeai'] if kind=='claude-code' else ['login','--device-auth']
assert sys.argv[1:]==expected,sys.argv
with open('/dev/tty') as terminal:
 assert os.isatty(terminal.fileno())
print('access_token=NEVER_PUBLIC_ACCESS refresh_token=NEVER_PUBLIC_REFRESH',flush=True)
print('Authorization: Bearer NEVER_PUBLIC_BEARER',flush=True)
print('https://auth.openai.com.evil/codex/device',flush=True)
if scenario.get('flood_login'):
 for i in range(150):print('message '+str(i)+' '+('x'*200),flush=True)
if scenario.get('child'):
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
 (root/'child.pid').write_text(str(child.pid))
if kind=='claude-code':
 print('https://claude.ai/oauth/authorize?client_id=fixture&code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fixture-state',flush=True)
 print('Paste code here: ',end='',flush=True)
 value=sys.stdin.readline().strip()
 print('Received '+value,flush=True)
 assert value=='expected-oauth-code'
else:
 print('https://auth.openai.com/codex/device',flush=True)
 print('Enter this code: ABCD-EFGH',flush=True)
 while not (root/'complete').exists():time.sleep(.01)
if scenario.get('false_success'):print('Login succeeded',flush=True);sys.exit(0)
state_path.write_text(json.dumps({'connected':True,'method':'claude.ai' if kind=='claude-code' else 'subscription'}))
print('Signed in',flush=True)
"""


@pytest.fixture
async def auth(tmp_path, monkeypatch):
    binary = tmp_path / "native-fixture"
    binary.write_text(f"#!{sys.executable}\n" + FIXTURE)
    binary.chmod(0o700)
    codex_binary = tmp_path / "codex-fixture"
    codex_binary.write_text(f"#!{sys.executable}\n" + FIXTURE)
    codex_binary.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-reuse")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-reuse")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-must-not-reuse")
    service = HarnessAuthService(
        tmp_path / "data",
        binaries={"claude-code": str(binary), "codex": str(codex_binary)},
        login_timeout_s=3,
        status_timeout_s=1,
    )
    yield service
    await service.aclose()


async def until(predicate):
    async with asyncio.timeout(4):
        while True:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(0.01)


async def state(service, harness, expected, instance="default"):
    current = await service.status(harness, instance)
    return current if current["state"] == expected else None


async def test_claude_manual_code_uses_private_pty_and_only_actual_subscription_status(auth):
    assert (await auth.status("claude-code"))["state"] == "auth_required"
    started = await auth.start("claude-code")
    assert started["state"] == "connecting"

    async def prompt():
        current = await auth.status("claude-code")
        return current if current.get("requires_code") and current.get("auth_url") else None

    current = await until(prompt)
    assert current["auth_url"].startswith("https://claude.ai/oauth/authorize?")
    assert current["awaiting_code"]
    for invalid in ("code\nexecute", "code\r", "code\x1b", "", "x" * 2049):
        with pytest.raises(ValueError):
            await auth.submit_code("claude-code", invalid)
    await auth.submit_code("claude-code", "expected-oauth-code")
    connected = await until(lambda: state(auth, "claude-code", "connected"))
    assert connected["auth_method"] == "subscription"
    public = json.dumps(connected)
    for secret in (
        "expected-oauth-code",
        "NEVER_PUBLIC_ACCESS",
        "NEVER_PUBLIC_REFRESH",
        "NEVER_PUBLIC_BEARER",
        "private@example.org",
    ):
        assert secret not in public
    assert "auth_url" not in connected and "device_code" not in connected
    profile = auth.profile_path("claude-code")
    calls = [json.loads(line) for line in (profile / "calls.jsonl").read_text().splitlines()]
    login = next(call for call in calls if call["argv"] == ["auth", "login", "--claudeai"])
    assert login["tty"] and not login["ambient_secrets"]
    assert all(Path(call["home"]).is_relative_to(auth.root) for call in calls)
    assert auth.root.stat().st_mode & 0o777 == 0o700
    assert profile.stat().st_mode & 0o777 == 0o700


async def test_codex_device_flow_is_idempotent_and_never_accepts_api_key_as_subscription(auth):
    profile = auth.profile_path("codex", "second")
    (profile / "state.json").write_text(json.dumps({"connected": True, "method": "api_key"}))
    status = await auth.status("codex", "second")
    assert status["state"] == "auth_required" and status["auth_method"] == "api_key"
    starts = await asyncio.gather(*(auth.start("codex", "second") for _ in range(6)))
    assert len({item["operation_id"] for item in starts}) == 1

    async def prompt():
        current = await auth.status("codex", "second")
        return current if current.get("device_code") else None

    current = await until(prompt)
    assert current["auth_url"] == "https://auth.openai.com/codex/device"
    assert current["device_code"] == "ABCD-EFGH" and not current["requires_code"]
    with pytest.raises(ValueError):
        await auth.submit_code("codex", "ABCD-EFGH", "second")
    (profile / "complete").touch()
    connected = await until(lambda: state(auth, "codex", "connected", "second"))
    assert connected["auth_method"] == "subscription"
    calls = [json.loads(line) for line in (profile / "calls.jsonl").read_text().splitlines()]
    assert sum(call["argv"] == ["login", "--device-auth"] for call in calls) == 1
    assert all(not call["ambient_secrets"] for call in calls)
    assert (await auth.status("codex", "other"))["state"] == "auth_required"
    assert "sk-NEVER_PUBLIC_KEY" not in json.dumps(connected)


async def test_zero_login_exit_without_actual_authenticated_status_is_failed(auth):
    profile = auth.profile_path("codex")
    (profile / "scenario.json").write_text(json.dumps({"false_success": True}))
    await auth.start("codex")

    async def prompt():
        return (await auth.status("codex")).get("device_code")

    await until(prompt)
    (profile / "complete").touch()
    result = await until(lambda: state(auth, "codex", "failed"))
    assert result["auth_method"] == "unknown"


async def test_cancel_and_disconnect_stop_process_group_and_clear_only_owned_profile(auth):
    profile = auth.profile_path("codex", "one")
    other = auth.profile_path("codex", "two")
    (other / "state.json").write_text(json.dumps({"connected": True, "method": "subscription"}))
    (profile / "scenario.json").write_text(json.dumps({"child": True}))
    await auth.start("codex", "one")

    async def child_started():
        return (profile / "child.pid").exists()

    await until(child_started)
    pid = auth._load(("codex", "one"))["pid"]
    canceled = await auth.cancel("codex", "one")
    assert canceled["state"] == "canceled" and not auth._processes
    assert _process_start(pid) is None
    assert (await auth.status("codex", "two"))["state"] == "connected"
    result = await auth.disconnect("codex", "two")
    assert result["state"] == "auth_required"
    assert (await auth.status("codex", "two"))["state"] == "auth_required"
    assert not (other / "state.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/codex/device",
        "https://auth.openai.com.evil/codex/device",
        "https://auth.openai.com@evil.test/codex/device",
        "https://auth.openai.com/redirect?next=https://evil.test",
        "https://auth.openai.com/codex/device?access_token=secret",
        "https://claude.ai/oauth/authorize?redirect_uri=https%3A%2F%2Fevil.test%2Fcallback",
    ],
)
def test_authentication_urls_cannot_point_at_untrusted_hosts_or_redirects(url):
    assert _safe_url("codex" if "openai" in url else "claude-code", url) is None


async def test_private_profiles_reject_traversal_and_symlinks(auth, tmp_path):
    for bad in ("../outside", "/tmp/outside", "", "..", "space value"):
        with pytest.raises(ValueError):
            auth.profile_path("codex", bad)
    target = auth.root / "codex" / "linked"
    target.parent.mkdir(exist_ok=True)
    target.symlink_to(tmp_path)
    with pytest.raises(ValueError):
        auth.profile_path("codex", "linked")


async def test_busy_profile_rejects_disconnect_and_start_without_spawning(auth):
    auth.busy = lambda *args: True
    with pytest.raises(ValueError, match="Stop the active"):
        await auth.start("codex")
    with pytest.raises(ValueError, match="Stop the active"):
        await auth.disconnect("codex")
    assert not auth._workers


async def test_timeout_is_durable_and_clears_partial_credentials(auth):
    auth.login_timeout_s = 0.12
    await auth.start("codex")
    profile = auth.profile_path("codex")
    (profile / "partial.credentials").write_text("never-publish")
    result = await until(lambda: state(auth, "codex", "failed"))
    assert result["reason"] == "login_timeout"
    await until(lambda: _empty_processes(auth))
    assert not (profile / "partial.credentials").exists()
    assert not result.get("auth_url") and not result["requires_code"]


async def _empty_processes(service):
    return not service._processes


async def test_two_service_instances_claim_one_native_login(auth):
    peer = HarnessAuthService(auth.data_dir, binaries=auth.binaries, login_timeout_s=3)
    try:
        claims = await asyncio.gather(auth.start("codex"), peer.start("codex"))
        assert claims[0]["operation_id"] == claims[1]["operation_id"]
        profile = auth.profile_path("codex")

        async def prompt():
            return (await peer.status("codex")).get("device_code")

        await until(prompt)
        calls = [json.loads(line) for line in (profile / "calls.jsonl").read_text().splitlines()]
        assert sum(call["argv"] == ["login", "--device-auth"] for call in calls) == 1
        # A second owner must not overwrite an active operation during recovery.
        await peer.recover()
        assert (await peer.status("codex"))["state"] == "connecting"
        assert (await peer.cancel("codex"))["state"] == "canceled"
        await until(lambda: _empty_processes(auth))
    finally:
        await peer.aclose()


async def test_recovery_kills_only_matching_owned_process_and_clears_profile(auth):
    key = ("codex", "interrupted")
    profile = auth.profile_path(*key)
    (profile / "partial.credentials").write_text("never-publish")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(30)", start_new_session=True
    )
    try:
        row = auth._load(key)
        row.update(
            state="connecting",
            operation_id="lost-operation",
            owner_pid=-1,
            owner_start="invalid",
            pid=process.pid,
            process_start=_process_start(process.pid),
            deadline=time.time() + 30,
        )
        auth._save(row)
        await auth.recover()
        await asyncio.wait_for(process.wait(), 1)
        assert process.returncode == -signal.SIGKILL
        assert not (profile / "partial.credentials").exists()
        result = await auth.status(*key)
        assert result["state"] == "failed" and result["reason"] == "login_interrupted"
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def test_recovery_never_kills_reused_pid(auth):
    key = ("codex", "reused")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(30)", start_new_session=True
    )
    try:
        row = auth._load(key)
        row.update(
            state="connecting",
            operation_id="lost-operation",
            owner_pid=-1,
            owner_start="invalid",
            pid=process.pid,
            process_start="wrong-start-time",
            deadline=time.time() + 30,
        )
        auth._save(row)
        await auth.recover()
        assert process.returncode is None and _process_start(process.pid)
        assert (await auth.status(*key))["state"] == "failed"
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def test_status_rejects_malformed_or_truncated_native_output(auth):
    for harness, scenario in (("claude-code", "invalid_status"), ("codex", "flood_status")):
        profile = auth.profile_path(harness)
        (profile / "scenario.json").write_text(json.dumps({scenario: True}))
        result = await auth.status(harness)
        assert result["state"] == "auth_required"
        assert result["reason"] == "native_status_unavailable"


async def test_native_binary_can_be_installed_after_service_start(auth):
    actual_binary = auth.binaries["codex"]
    selected = [None]
    auth.binaries["codex"] = lambda: selected[0]
    assert (await auth.status("codex"))["available"] is False
    assert (await auth.start("codex"))["reason"] == "native_cli_missing"
    selected[0] = actual_binary
    assert (await auth.start("codex"))["state"] == "connecting"
    await auth.cancel("codex")


async def test_server_path_snapshot_resolves_env_shebang_runtime(auth, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime-bin"
    runtime.mkdir()
    (runtime / "fixture-python").symlink_to(sys.executable)
    binary = tmp_path / "runtime-native"
    binary.write_text("#!/usr/bin/env fixture-python\n" + FIXTURE)
    binary.chmod(0o700)
    service = HarnessAuthService(
        tmp_path / "runtime-data", binaries={"codex": str(binary)}, path=str(runtime)
    )
    monkeypatch.setenv("PATH", "/path-from-an-unrelated-ui-process")
    try:
        assert (await service.status("codex"))["state"] == "auth_required"
        assert (await service.start("codex"))["state"] == "connecting"

        async def prompt():
            return (await service.status("codex")).get("device_code")

        await until(prompt)
        (service.profile_path("codex") / "complete").touch()
        assert (await until(lambda: state(service, "codex", "connected")))[
            "auth_method"
        ] == "subscription"
    finally:
        await service.aclose()


async def test_authentication_log_is_bounded_without_losing_verified_prompt(auth):
    profile = auth.profile_path("codex")
    (profile / "scenario.json").write_text(json.dumps({"flood_login": True}))
    await auth.start("codex")

    async def prompt():
        result = await auth.status("codex")
        return result if result.get("device_code") else None

    result = await until(prompt)
    assert result["auth_url"] == "https://auth.openai.com/codex/device"
    assert len(result["log"]) <= 40 and sum(map(len, result["log"])) <= 8000
    assert "NEVER_PUBLIC" not in json.dumps(result)


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_real_studio_account_endpoints_require_operator_and_use_native_status(
    auth, tmp_path, monkeypatch, harness
):
    from worldd.app import create_app
    from worldd.studio_gateway import create_studio_gateway

    class Gateway:
        async def aclose(self):
            pass

    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "auth-fixture-operator")
    monkeypatch.setenv("WORLD_CLAUDE_CLI", auth.binaries["claude-code"])
    monkeypatch.setenv("WORLD_CODEX_CLI", auth.binaries["codex"])
    control = create_app(tmp_path / "app", gateway=Gateway())
    service = control.state.world.services["harnesses"]
    try:
        # Exercise the real HarnessService-owned auth object and its dynamic
        # binary callbacks, not a mocked readiness flag.
        native_auth = service.auth
        assert native_auth is control.state.world.services["harness_auth"]
        app = create_studio_gateway(control, public=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://studio.example"
        ) as client:
            path = f"/api/harnesses/{harness}"
            origin = {"Origin": "https://studio.example"}
            denied = await client.post(path + "/connect", json={}, headers=origin)
            assert denied.status_code == 401
            assert not native_auth._workers
            accepted = await client.post(
                "/api/session", json={"token": "auth-fixture-operator"}, headers=origin
            )
            assert accepted.status_code == 200
            disconnected = await client.get(path + "/connection")
            assert disconnected.status_code == 200
            assert disconnected.json()["state"] == "disconnected"
            assert (await client.post(path + "/connect", json={})).status_code == 403
            result = await client.post(path + "/connect", json={}, headers=origin)
            assert result.status_code == 202 and result.json()["state"] == "connecting"

            async def prompt():
                current = (await client.get(path + "/connection")).json()
                return current if current.get("auth_url") else None

            current = await until(prompt)
            assert current["state"] == "connecting"
            if harness == "claude-code":

                async def manual_prompt():
                    current = (await client.get(path + "/connection")).json()
                    return current if current.get("requires_code") else None

                await until(manual_prompt)
                invalid = await client.post(
                    path + "/connection/code", json={"code": "secret\ncommand"}, headers=origin
                )
                assert invalid.status_code == 400 and "secret" not in invalid.text
                submitted = await client.post(
                    path + "/connection/code", json={"code": "expected-oauth-code"}, headers=origin
                )
                assert submitted.status_code == 200
            else:
                assert current["device_code"] == "ABCD-EFGH"
                (native_auth.profile_path(harness) / "complete").touch()

            async def connected():
                current = (await client.get(path + "/connection")).json()
                return current if current["state"] == "connected" else None

            current = await until(connected)
            assert current["auth_method"] == "subscription"
            assert "NEVER_PUBLIC" not in json.dumps(current)
            assert "expected-oauth-code" not in json.dumps(current)
            library = (await client.get("/api/harnesses")).json()["harnesses"]
            item = next(item for item in library if item["id"] == harness)
            subscription = next(mode for mode in item["modes"] if mode["id"] == "subscription")
            assert subscription["ready"] and not subscription["billing"]["usd_metered"]
            result = await client.post(path + "/disconnect", json={}, headers=origin)
            assert result.status_code == 200 and result.json()["state"] == "disconnected"
    finally:
        await control.state.world.aclose()
