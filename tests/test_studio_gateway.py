"""Single-origin mounts retain distinct operator and run capabilities."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from worldd.__main__ import _arguments, _configure_studio
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.security import SESSION_COOKIE, SESSION_SECONDS, protect_researcher
from worldd.studio_gateway import create_studio_gateway


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "operator-fixture-secret")
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "provider-fixture-secret")
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "studio-model",
                "choices": [{"message": {"role": "assistant", "content": "fixture"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    prices = {
        "models": {
            "studio-model": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 4096,
                "max_output_tokens": 1024,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model:
        control = create_app(tmp_path, gateway=Gateway(), lane_client=model, prices=prices)

        app = create_studio_gateway(control, public=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://studio.example"
        ) as client:
            yield app, client, calls
        await control.state.world.aclose()


async def login(client):
    return await client.post(
        "/api/session",
        json={"token": "operator-fixture-secret"},
        headers={"Origin": "https://studio.example"},
    )


async def test_browser_bootstrap_login_cookie_and_logout(studio):
    _app, client, _calls = studio
    page = await client.get("/")
    assert page.status_code == 200
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "connect-src 'self'" in page.headers["content-security-policy"]
    assert "base-uri 'none'" in page.headers["content-security-policy"]
    assert (await client.get("/assets/studio.js")).status_code == 200
    assert (await client.get("/api/session")).json() == {
        "authenticated": False,
        "requires_token": True,
    }
    assert (await client.get("/v1/runs")).status_code == 401
    assert (await client.get("/healthz")).status_code == 401
    assert (await client.get("/bridge")).status_code == 401
    wrong = await client.post(
        "/api/session", json={"token": "wrong"}, headers={"Origin": "https://studio.example"}
    )
    assert wrong.status_code == 401 and SESSION_COOKIE not in client.cookies
    oversized = await client.post(
        "/api/session",
        json={"token": "x" * 16384},
        headers={"Origin": "https://studio.example"},
    )
    assert oversized.status_code == 413 and "x" * 100 not in oversized.text
    assert (
        await client.post(
            "/api/session",
            content=b'{"token":"\\ud800"}',
            headers={"Origin": "https://studio.example", "Content-Type": "application/json"},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/session",
            json={"token": "incorrect-☃"},
            headers={"Origin": "https://studio.example"},
        )
    ).status_code == 401
    accepted = await login(client)
    assert accepted.status_code == 200 and accepted.json()["authenticated"] is True
    cookie = accepted.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert "Path=/" in cookie and str(SESSION_SECONDS) in cookie
    assert "operator-fixture-secret" not in cookie + accepted.text
    assert (await client.get("/api/session")).json()["authenticated"] is True
    assert (await client.get("/v1/runs")).status_code == 200
    saved = client.cookies.get(SESSION_COOKIE)
    logout = await client.delete("/api/session", headers={"Origin": "https://studio.example"})
    assert logout.json() == {"authenticated": False, "requires_token": True}
    assert SESSION_COOKIE not in client.cookies
    assert (
        await client.get("/v1/runs", headers={"cookie": f"{SESSION_COOKIE}={saved}"})
    ).status_code == 401


async def test_cookie_mutations_require_same_origin_and_header_clients_remain_compatible(studio):
    _app, client, _calls = studio
    assert (await login(client)).status_code == 200
    for headers in (
        {},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://studio.example"},
    ):
        assert (await client.post("/v1/run", json={}, headers=headers)).status_code == 403
    created = await client.post("/v1/run", json={}, headers={"Origin": "https://studio.example"})
    assert created.status_code == 201
    assert (
        await client.post("/v1/run", json={}, headers={"Referer": "https://studio.example/studio"})
    ).status_code == 201
    assert (
        await client.delete("/api/session", headers={"Origin": "https://evil.example"})
    ).status_code == 403
    # An explicitly supplied wrong capability cannot borrow operator cookie authority.
    assert (
        await client.post("/v1/run", json={}, headers={"Authorization": "Bearer run-only"})
    ).status_code == 401
    client.cookies.clear()
    assert (
        await client.post(
            "/v1/run", json={}, headers={"X-World-Control-Token": "operator-fixture-secret"}
        )
    ).status_code == 201
    assert (
        await client.post("/api/session", json={"token": "operator-fixture-secret"})
    ).status_code == 403


async def test_scoped_mounts_bypass_control_auth_without_gaining_control(studio):
    app, client, calls = studio
    world = app.state.world
    first = world.create_run(RunConfig())
    second = world.create_run(RunConfig())
    headers = {"Authorization": "Bearer " + first["run_token"]}
    assert (
        await client.get("/agent/v1/status", params={"run_id": first["run_id"]}, headers=headers)
    ).status_code == 200
    assert (
        await client.get("/agent/v1/status", params={"run_id": second["run_id"]}, headers=headers)
    ).status_code == 401
    assert (await client.get("/agent/v1/runs", headers=headers)).status_code == 404
    assert (await client.post("/agent/v1/run", json={}, headers=headers)).status_code == 404
    assert (await client.get("/v1/runs", headers=headers)).status_code == 401
    assert (await client.get("/model/v1/runs", headers=headers)).status_code == 404
    body = {
        "model": "studio-model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 10,
    }
    endpoint = "/model/v1/chat/completions"
    assert (
        await client.post(endpoint, json=body, headers={"X-World-Run-Id": first["run_id"]})
    ).status_code == 401
    reply = await client.post(
        endpoint, json=body, headers={**headers, "X-World-Run-Id": first["run_id"]}
    )
    assert reply.status_code == 200, reply.text
    assert len(calls) == 1 and sum(world.store.spend(first["run_id"]).values()) > 0
    assert (
        await client.post(
            endpoint, json=body, headers={**headers, "X-World-Run-Id": second["run_id"]}
        )
    ).status_code == 401
    assert (await client.get("/public/v1/runs")).status_code == 404
    assert (await client.post("/public/v1/run", json={})).status_code == 404
    world.services["identity"].create(
        principal="Fixture operator",
        name="Disclosed fixture",
        mailbox="agent@example.org",
        homepage="https://studio.example/public/fixture-agent",
        run_id=first["run_id"],
    )
    assert (await client.get("/public/fixture-agent")).status_code == 200
    assert (
        await client.post("/public/fixture-agent/incident", json={"report": "Fixture report"})
    ).status_code == 202
    # An operator browser cookie is never a run or model capability.
    await login(client)
    assert (
        await client.get("/agent/v1/status", params={"run_id": first["run_id"]})
    ).status_code == 401
    assert (
        await client.post(endpoint, json=body, headers={"X-World-Run-Id": first["run_id"]})
    ).status_code == 401


async def test_session_expiry_and_control_key_rotation_revoke_browser_authority(
    studio, monkeypatch
):
    app, client, _calls = studio
    assert (await login(client)).status_code == 200
    sessions = app.state.control_app.state.browser_sessions
    for key, (_, fingerprint) in sessions.sessions.items():
        sessions.sessions[key] = (0, fingerprint)
    assert (await client.get("/v1/runs")).status_code == 401
    assert (await login(client)).status_code == 200
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "rotated-fixture-secret")
    assert (await client.get("/api/session")).json()["authenticated"] is False
    assert (await client.get("/v1/runs")).status_code == 401


async def test_actual_studio_settings_are_write_only_and_invalid_inputs_never_echo_keys(
    studio, monkeypatch
):
    app, client, _calls = studio
    service = app.state.control_app.state.studio

    async def empty_library():
        return {"items": []}

    monkeypatch.setattr(service.harnesses, "library", empty_library)
    assert (await client.get("/api/settings")).status_code == 401
    await login(client)
    current = (await client.get("/api/settings")).json()
    secret = "fixture-private-provider-key-never-in-HTTP-output"
    headers = {"Origin": "https://studio.example"}
    saved = await client.post(
        "/api/settings",
        headers=headers,
        json={"revision": current["revision"], "credentials": {"openai_api_key": secret}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["credentials"]["openai"]["configured"] is True
    assert secret not in saved.text
    assert service.settings.resolve("WORLD_OPENAI_API_KEY") == secret
    malformed = await client.post(
        "/api/settings",
        headers=headers,
        json={"credentials": {"openai_api_key": secret}, secret: secret},
    )
    assert malformed.status_code == 400 and secret not in malformed.text
    invalid_type = await client.post(
        "/api/settings",
        headers=headers,
        json={"credentials": {"openai_api_key": {"secret": secret}}},
    )
    assert invalid_type.status_code == 400 and secret not in invalid_type.text
    assert secret not in (await client.get("/api/settings")).text
    client.cookies.clear()
    for path in (
        "/assets/%2e%2e%2fapi/settings",
        "/assets/%2e%2e/%2e%2e/api/settings",
        "/agent/..%2fapi/settings",
        "/model/..%2fapi/settings",
    ):
        response = await client.get(path)
        assert response.status_code in {400, 401, 404}, (path, response.status_code)
        assert secret not in response.text


async def test_control_model_aliases_still_require_operator_authority(studio):
    app, client, calls = studio
    run = app.state.world.create_run(RunConfig())
    body = {
        "model": "studio-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    }
    for path in ("/openai/v1/chat/completions", "/v1/chat/completions"):
        denied = await client.post(
            path,
            json=body,
            headers={
                "Authorization": "Bearer " + run["run_token"],
                "X-World-Run-Id": run["run_id"],
            },
        )
        assert denied.status_code == 401
    assert not calls
    await login(client)
    assert (
        await client.post(
            "/openai/v1/chat/completions", json=body, headers={"X-World-Run-Id": run["run_id"]}
        )
    ).status_code == 403
    assert not calls


async def test_no_token_loopback_bootstrap_and_legacy_protection_compatibility(monkeypatch):
    monkeypatch.delenv("WORLD_CONTROL_TOKEN", raising=False)
    control = FastAPI()
    protect_researcher(control)
    control.state.world = SimpleNamespace()
    control.state.lane_router = APIRouter()

    @control.get("/healthz")
    async def health():
        return {"ok": True}

    @control.post("/api/write")
    async def write():
        return {"ok": True}

    app = create_studio_gateway(control)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/session")).json() == {
            "authenticated": True,
            "requires_token": False,
        }
        assert (
            await client.post("/api/write", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await client.post("/api/write", headers={"Sec-Fetch-Site": "cross-site"})
        ).status_code == 403
        assert (await client.post("/api/write")).status_code == 200
        assert (
            await client.post("/api/write", headers={"Host": "rebound.example"})
        ).status_code == 400
        assert (
            await client.post(
                "/api/write",
                headers={"Host": "rebound.example", "Origin": "http://rebound.example"},
            )
        ).status_code == 400
        assert (
            await client.post(
                "/api/write", headers={"Host": "rebound.example", "X-Forwarded-Host": "localhost"}
            )
        ).status_code == 400
        assert (
            await client.get("/api/session", headers={"Host": "rebound.example"})
        ).status_code == 400
        for host in ("evil@localhost", "localhost/path", "[", "localhost:invalid"):
            assert (await client.post("/api/write", headers={"Host": host})).status_code == 400
        assert (await client.get("/public/anything")).status_code == 404
    legacy = FastAPI()
    protect_researcher(legacy)
    legacy.add_api_route("/healthz", health)
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-control")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(legacy), base_url="http://legacy"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/api/session")).status_code == 401


async def test_wrapper_owns_control_lifespan_once():
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("started")
        yield
        events.append("closed")

    control = FastAPI(lifespan=lifespan)
    control.state.world = SimpleNamespace()
    control.state.lane_router = APIRouter()
    app = create_studio_gateway(control)
    async with app.router.lifespan_context(app):
        assert events == ["started"]
    assert events == ["started", "closed"]


def test_default_single_http_origin_legacy_flags_and_bind_guard(monkeypatch):
    monkeypatch.delenv("WORLD_CONTROL_TOKEN", raising=False)
    defaults = _arguments([])
    assert defaults.port == 7777 and not defaults.legacy_listeners
    assert defaults.agent_port == defaults.lane_port == defaults.public_port == 0
    assert defaults.proxy_port == 8080
    assert defaults.proxy_host == "127.0.0.1"
    assert _arguments(["--public"]).public
    no_proxy = _arguments(["--no-proxy"])
    assert no_proxy.proxy_port == 0 and not no_proxy.legacy_listeners
    legacy = _arguments(["--legacy-listeners"])
    assert legacy.agent_port == 7778 and legacy.lane_port == 8081
    assert _arguments(["--agent-port", "0"]).legacy_listeners
    assert _arguments(["--proxy-port", "0"]).legacy_listeners
    internal = _arguments(["--proxy-host", "0.0.0.0"])
    assert not internal.legacy_listeners and internal.proxy_host == "0.0.0.0"
    for host in ("0.0.0.0", "::", "192.0.2.10", "example.org"):
        with pytest.raises(SystemExit):
            _arguments(["--host", host])
    assert _arguments(["--host", "::1"]).host == "::1"
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "configured-operator")
    assert _arguments(["--host", "0.0.0.0"]).host == "0.0.0.0"
    for flags in (["--port", "0"], ["--lane-port", "7777"], ["--public", "--legacy-listeners"]):
        with pytest.raises(SystemExit):
            _arguments(flags)


def test_studio_internal_urls_follow_actual_listener_ports_before_startup(monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-control")
    calls = []
    control = SimpleNamespace(
        state=SimpleNamespace(
            studio=SimpleNamespace(
                configure_urls=lambda origin, **kwargs: calls.append((origin, kwargs))
            )
        )
    )
    _configure_studio(control, _arguments(["--host", "0.0.0.0", "--port", "7000"]))
    assert calls[-1] == ("http://127.0.0.1:7000", {})
    _configure_studio(
        control,
        _arguments(
            ["--host", "::", "--port", "7000", "--agent-port", "7001", "--lane-port", "7002"]
        ),
    )
    assert calls[-1] == (
        "http://[::1]:7000",
        {"agent_url": "http://[::1]:7001", "lane_url": "http://[::1]:7002"},
    )
