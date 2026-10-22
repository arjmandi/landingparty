import asyncio

import pytest
from capability_helpers import CapabilityWorld

from worldd.browser import BrowserService, PlaywrightBackend


class Backend:
    def __init__(self):
        self.handles = []
        self.pause = None
        self.started = asyncio.Event()
        self.challenge = None
        self.actions = []

    async def open(self, credential, *, proxy=None, user_agent=None):
        handle = {"url": "about:blank", "credential": credential, "closed": False}
        self.handles.append(handle)
        return handle

    async def goto(self, handle, url):
        self.started.set()
        if self.pause:
            await self.pause.wait()
        handle["url"] = url
        return {"url": url, "status": 200}

    async def current_url(self, handle):
        return handle["url"]

    async def snapshot(self, handle, view):
        result = {"url": handle["url"], "site_tools": []}
        if view in {"tree", "both"}:
            result["tree"] = "Fixture page fixture-secret-value"
        if view in {"screenshot", "both"}:
            result["screenshot"] = b"fixture-png"
        return result

    async def action(self, handle, tool, params, *, human=False):
        self.actions.append({"tool": tool, "params": params, "human": human})
        return {"url": handle["url"]}

    async def login(self, handle, login):
        handle["url"] = login["url"]

    async def needs_human(self, handle):
        return self.challenge

    async def close(self, handle):
        handle["closed"] = True
        return {"cookies": [], "origins": []}


@pytest.fixture
def world(tmp_path):
    result = CapabilityWorld(tmp_path)
    yield result
    result.close()


def browser(world):
    backend = Backend()
    service = BrowserService(world, backend=backend, clock=world.clock)
    world.services["browser"] = service
    return service, backend


async def open_session(world, service, run_id, alias="fixture"):
    if alias:
        world.provision(run_id)
    return (await service.handle(run_id, "a_open", "open_session", {"alias": alias}))["session_id"]


async def test_actual_backend_never_silently_disables_sandbox_or_tls():
    backend = PlaywrightBackend()
    assert backend.sandbox is True
    # Actual browser launch is an environment smoke check, not replaced by this
    # transport fixture suite. The adapter supplies chromium_sandbox=True.


async def test_operator_return_in_flight_is_drained_before_seal(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    backend.challenge = "mfa"
    pending = await service.handle(
        run_id, "a_doc", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
    )
    world.services["asks"].answer(pending["ticket_id"], "approve")
    service.takeover_start(session_id, pending["ticket_id"])
    started = asyncio.Event()

    async def paused_url(handle):
        started.set()
        await asyncio.Event().wait()

    backend.current_url = paused_url
    task = asyncio.create_task(service.takeover_done(session_id))
    await started.wait()
    world.store.update_run(run_id, state="stopping")
    await asyncio.wait_for(service.stop_run(run_id), 2)
    assert task.cancelled()
    assert backend.handles[0]["closed"]
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    assert world.store.records(run_id)[-1]["kind"] == "seal"
    assert not service._operations
    await service.aclose()


async def test_human_actions_serialize_and_stop_cancels_them(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    pending = world.services["asks"].raise_ticket(
        run_id, "a_take", "takeover", pending_action={"session_id": session_id}
    )
    world.services["asks"].answer(pending["ticket_id"], "approve")
    service.takeover_start(session_id, pending["ticket_id"])
    started = asyncio.Event()

    async def paused_action(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    backend.action = paused_action
    task = asyncio.create_task(
        service.human_action(
            session_id, "type", {"selector": "input", "text": "private-human-password"}
        )
    )
    await started.wait()
    with pytest.raises(ValueError, match="another human action"):
        await service.human_action(session_id, "click", {"selector": "button"})
    await asyncio.wait_for(service.stop_run(run_id), 2)
    assert task.cancelled()
    assert "private-human-password" not in str(world.store.records(run_id))
    await service.aclose()


async def test_captcha_refusal_is_not_overridden_by_model_token_threshold(world):
    run_id = world.run(captcha_policy="refuse", breaker_policy={"challenge_tokens": 10})
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    backend.challenge = "captcha"
    result = await service.handle(
        run_id,
        "a_captcha",
        "goto",
        {"session_id": session_id, "url": "https://fixture.test/challenge"},
    )
    assert result["rule_id"] == "captcha_policy"
    assert service.observe_model_usage(run_id, 1000, call_id="actual_model_call") == []
    assert world.services["asks"].list_tickets(run_id) == []
    await service.aclose()


async def test_context_is_reused_and_account_actions_are_serialized(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    assert (await service.handle(run_id, "a_reopen", "open_session", {"alias": "fixture"}))[
        "session_id"
    ] == session_id
    backend.pause = asyncio.Event()
    pending = asyncio.create_task(
        service.handle(
            run_id, "a_goto", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
        )
    )
    await backend.started.wait()
    concurrent = await service.handle(run_id, "a_snap", "snapshot", {"session_id": session_id})
    assert concurrent["type"] == "account_busy"
    backend.pause.set()
    assert (await pending)["kind"] == "result"
    assert len(backend.handles) == 1
    await service.aclose()


async def test_same_account_cannot_be_opened_by_two_runs(world):
    run_id = world.run()
    service, _ = browser(world)
    await open_session(world, service, run_id)
    result = await service.handle(world.run(), "a_other", "open_session", {"alias": "fixture"})
    assert result["type"] == "account_busy"
    await service.aclose()


async def test_snapshots_redact_known_credentials_and_save_screenshots_in_world(world):
    run_id = world.run()
    service, _ = browser(world)
    session_id = await open_session(world, service, run_id)
    output = await service.handle(
        run_id, "a_snapshot", "snapshot", {"session_id": session_id, "view": "both"}
    )
    assert "fixture-secret-value" not in output["tree"]
    path = (
        world.store.run_dir(run_id) / "world" / output["screenshot_handle"].removeprefix("/world/")
    )
    assert path.read_bytes() == b"fixture-png"
    assert any(record.get("modality") == "both" for record in world.store.records(run_id))
    await service.aclose()


async def test_browser_time_is_metered_and_idle_close_is_real(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id, alias=None)
    world.clock.advance(60)
    await service.handle(run_id, "a_snapshot", "snapshot", {"session_id": session_id})
    assert world.store.spend(run_id)["browser"] == 0.002
    world.clock.advance(301)
    await service.check_idle()
    assert backend.handles[0]["closed"]
    assert service.list_sessions(run_id) == []
    assert world.store.spend(run_id)["browser"] == 0.012033334
    assert world.store.reserved(run_id) == 0
    assert world.store.reconcile(run_id)["valid"]


async def test_takeover_hides_human_keystrokes_and_refreshes_agent_observation(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    backend.challenge = "challenge"
    pending = await service.handle(
        run_id, "a_goto", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
    )
    assert pending["state"] == "needs_human"
    world.services["asks"].answer(pending["ticket_id"], "approve")
    service.takeover_start(session_id, pending["ticket_id"])
    before = world.store.total_spent(run_id)
    blocked = await service.handle(run_id, "a_blocked", "snapshot", {"session_id": session_id})
    assert blocked["type"] == "human_holds_session" and world.store.total_spent(run_id) == before
    await service.human_action(
        session_id, "type", {"selector": "#password", "text": "human-private-password"}
    )
    world.clock.advance(21)
    world.services["asks"].touch(pending["ticket_id"])
    world.clock.advance(21)
    backend.challenge = None
    assert (await service.takeover_done(session_id))["fresh_observation_required"]
    output = await service.handle(run_id, "a_fresh", "snapshot", {"session_id": session_id})
    assert output["kind"] == "result"
    assert "human-private-password" not in str(world.store.records(run_id))
    assert world.services["asks"].human_spend(run_id)["seconds"] == 42
    assert [record["kind"] for record in world.store.records(run_id)].count("takeover_start") == 1
    await service.aclose()


async def test_browser_network_writes_require_scoped_exact_action_approval(world):
    run_id = world.run()
    service, _ = browser(world)
    session_id = await open_session(world, service, run_id)
    session = service.sessions[session_id]
    token = session["token"]
    denied = await service.authorize_network_request(
        run_id, "POST", "https://fixture.test/post", {}, b"payload", token=token
    )
    assert not denied["allow"]
    params = {
        "session_id": session_id,
        "selector": "#post",
        "scope": "post",
        "url": "https://fixture.test/post",
    }
    pending = await service.handle(run_id, "a_post", "click", params)
    assert pending["kind"] == "ticket"
    world.services["asks"].answer(pending["ticket_id"], "approve", ask_class="approval")
    assert (await service.handle(run_id, "a_retry", "click", params))["kind"] == "result"
    session["active"] = {
        "scope": "post",
        "action_id": "a_retry",
        "lease_id": "lease_fixture",
        "request": {"url": "https://fixture.test/post"},
    }
    assert (
        await service.authorize_network_request(
            run_id, "POST", "https://fixture.test/post", {}, b"payload", token=token
        )
    )["allow"]
    assert not (
        await service.authorize_network_request(
            run_id, "POST", "https://other.test/post", {}, b"payload", token=token
        )
    )["allow"]
    session["active"] = None
    await service.aclose()


async def test_anonymous_network_is_read_only_and_token_bound(world):
    run_id = world.run()
    service, _ = browser(world)
    session_id = await open_session(world, service, run_id, alias=None)
    token = service.sessions[session_id]["token"]
    decision = await service.authorize_network_request(
        run_id, "GET", "https://fixture.test/", {}, b"", token=token
    )
    assert decision["allow"] and decision["response_private"]
    assert not (
        await service.authorize_network_request(
            run_id, "POST", "https://fixture.test/", {}, b"", token=token
        )
    )["allow"]
    assert not (
        await service.authorize_network_request(
            run_id, "GET", "https://fixture.test/", {}, b"", token="wrong"
        )
    )["allow"]
    await service.aclose()


async def test_upload_cannot_read_private_world_files_or_escape_workspace(world):
    run_id = world.run()
    service, _ = browser(world)
    session_id = await open_session(world, service, run_id, alias=None)
    result = await service.handle(
        run_id,
        "a_upload",
        "upload",
        {"session_id": session_id, "selector": "#file", "handle": "../record.jsonl"},
    )
    assert result["kind"] == "blocked" and result["rule_id"] == "browser_scope"
    await service.aclose()


async def test_stopping_browser_closes_sessions_and_releases_metering_hold(world):
    run_id = world.run()
    service, backend = browser(world)
    await open_session(world, service, run_id, alias=None)
    world.clock.advance(10)
    world.store.update_run(run_id, state="stopping")
    await service.stop_run(run_id)
    assert backend.handles[0]["closed"]
    assert world.store.reserved(run_id) == 0
    assert world.store.records(run_id)[-1]["kind"] == "session_close"


def test_browser_child_has_private_directories_and_no_provider_or_proxy_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "fixture-world-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-harness-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture-private-proxy")
    backend = PlaywrightBackend(state_dir=tmp_path / "browser")
    child = backend.child_environment()
    assert not ({"WORLD_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY"} & child.keys())
    assert child["HOME"] == str(tmp_path / "browser")
    assert child["XDG_CONFIG_HOME"].startswith(child["HOME"])


async def test_private_nss_trust_import_preserves_exact_world_ca(tmp_path):
    from worldd.proxy import CertificateAuthority

    authority = CertificateAuthority(tmp_path / "ca")
    backend = PlaywrightBackend(state_dir=tmp_path / "browser")
    await backend.trust_ca(authority.cert_path)
    await backend.trust_ca(authority.cert_path)
    assert (backend.state_dir / ".pki/nssdb/cert9.db").is_file()


async def test_revocation_stops_background_authenticated_requests(world):
    run_id = world.run()
    service, _ = browser(world)
    session_id = await open_session(world, service, run_id)
    token = service.sessions[session_id]["token"]
    world.services["vault"].revoke_account("fixture")
    decision = await service.authorize_network_request(
        run_id, "GET", "https://fixture.test/", {}, b"", token=token
    )
    assert not decision["allow"] and decision["rule_id"] == "account_revoked"
    await service.aclose()
    assert world.services["vault"].get_account("fixture")["state"] == "revoked"


async def test_challenge_token_breaker_uses_actual_tokens_once_and_never_runs_after_seal(world):
    run_id = world.run(breaker_policy={"challenge_tokens": 100})
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    backend.challenge = "challenge"
    pending = await service.handle(
        run_id, "a_goto", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
    )
    assert pending["kind"] == "ticket"
    assert service.observe_model_usage(run_id, 60, call_id="call_one") == []
    assert service.observe_model_usage(run_id, 60, call_id="call_one") == []
    triggered = service.observe_model_usage(run_id, 41, call_id="call_two")
    assert triggered[0]["actual_tokens"] == 101
    assert service.observe_model_usage(run_id, 300, call_id="call_three") == []
    blocked = await service.handle(
        run_id, "a_try_puzzle", "click", {"session_id": session_id, "selector": "#puzzle"}
    )
    assert blocked["rule_id"] == "browser_human_required" and backend.actions == []
    assert len(world.services["asks"].list_tickets(run_id)) == 1
    await service.stop_run(run_id)
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    before = world.store.records(run_id)
    assert service.observe_model_usage(run_id, 1000, call_id="call_late") == []
    assert world.store.records(run_id) == before
    await service.aclose()


async def test_background_account_reads_cannot_escape_its_url_scope(world):
    run_id = world.run(packs=["browse"])
    service, _ = browser(world)
    world.provision(run_id, scope_rules={"read": ["https://fixture.test/docs"]})
    opened = await service.handle(run_id, "a_open", "open_session", {"alias": "fixture"})
    session = service.sessions[opened["session_id"]]
    allowed = await service.authorize_network_request(
        run_id, "GET", "https://fixture.test/docs/one", {}, b"", token=session["token"]
    )
    denied = await service.authorize_network_request(
        run_id, "GET", "https://fixture.test/account/keys", {}, b"", token=session["token"]
    )
    assert allowed["allow"] and allowed["response_private"]
    assert not denied["allow"] and not denied["allow_credentials"]
    await service.aclose()


async def test_site_tool_metadata_and_urls_cannot_echo_vault_secrets(world):
    run_id = world.run()
    service, backend = browser(world)
    session_id = await open_session(world, service, run_id)
    original = backend.snapshot

    async def snapshot(handle, view):
        result = await original(handle, view)
        result.update(
            url="https://fixture.test/?echo=fixture-secret-value",
            site_tools=[
                {"name": "fixture-secret-value", "url": "https://fixture.test/fixture-secret-value"}
            ],
        )
        return result

    backend.snapshot = snapshot
    reply = await service.handle(run_id, "a_snap", "snapshot", {"session_id": session_id})
    assert "fixture-secret-value" not in str(reply)
    assert "[redacted]" in str(reply)
    await service.aclose()


async def test_supplemental_browser_takeover_rechecks_original_scoped_clearance(world):
    from capability_helpers import activate_browser_grant

    run_id = world.run(packs=["vault"])
    service, backend = browser(world)
    world.provision(run_id)
    grants, _ = await activate_browser_grant(world, run_id, prefix="/docs", max_uses=2)
    opened = await service.handle(
        run_id,
        "a_open",
        "open_session",
        {"alias": "fixture", "target_url": "https://fixture.test/docs"},
    )
    session_id = opened["session_id"]
    backend.challenge = "challenge"
    pending = await service.handle(
        run_id, "a_goto", "goto", {"session_id": session_id, "url": "https://fixture.test/docs/one"}
    )
    world.services["asks"].answer(pending["ticket_id"], "approve")
    service.takeover_start(session_id, pending["ticket_id"])
    session = service.sessions[session_id]
    session["active"] = {"scope": "settings", "human": True}
    try:
        allowed = await service.authorize_network_request(
            run_id, "POST", "https://fixture.test/docs/mfa", {}, b"", token=session["token"]
        )
        denied = await service.authorize_network_request(
            run_id, "POST", "https://fixture.test/settings/payment", {}, b"", token=session["token"]
        )
        assert allowed["allow"] and allowed["credential_body"]
        assert not denied["allow"]
    finally:
        session["active"] = None
    await service.aclose()
    await grants.aclose()
