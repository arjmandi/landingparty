"""Operator routes join real world state, credential storage and scoped model billing."""

import httpx
import pytest

from worldd.app import create_app
from worldd.harness_config import BUILTINS
from worldd.models import RunConfig
from worldd.studio_gateway import create_studio_gateway


class Gateway:
    async def aclose(self):
        pass


def price(rate="1"):
    return {
        "provider": "openai",
        "input": rate,
        "output": rate,
        "cache_read": "0.1",
        "cache_write": rate,
        "context_window": 4096,
        "max_output_tokens": 256,
        "source": "https://platform.openai.com/docs/pricing",
    }


@pytest.fixture
async def product(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "operator-test-only")
    requests, launches = [], []

    def provider(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "studio-model",
                "choices": [{"message": {"role": "assistant", "content": "local fixture"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        control = create_app(
            tmp_path,
            gateway=Gateway(),
            lane_client=upstream,
            prices={
                "models": {
                    "studio-model": price(),
                    "claude-studio": {**price(), "provider": "anthropic"},
                    "claude-studio-next": {**price(), "provider": "anthropic"},
                }
            },
        )
        world, studio = control.state.world, control.state.studio
        connection = {"state": "auth_required", "auth_method": "unknown"}

        async def library(refresh=False, instance_id="default"):
            return {
                "items": [
                    {
                        "id": name,
                        "name": name,
                        "installed": True,
                        "version": "1.0.0",
                        "api_ready": bool(world.credential(binding)),
                        "auth_modes": ["api_key", "subscription"],
                        "subscription_auth": dict(connection),
                        "requirements": {"node": True, "npm": True},
                        # The code-owned manifest's metadata, as the real library reports it.
                        "kind": manifest.kind,
                        "provider": manifest.provider,
                        "efforts": list(manifest.efforts),
                        "transcript": manifest.transcript,
                        "description": manifest.description,
                    }
                    for name, binding in (
                        ("codex", "WORLD_OPENAI_API_KEY"),
                        ("claude-code", "WORLD_ANTHROPIC_API_KEY"),
                    )
                    for manifest in BUILTINS
                    if manifest.id == name
                ]
            }

        async def launch(info, harness_id, *, auth_mode, model):
            launches.append((info, harness_id, auth_mode, model))
            return {"run_id": info["run_id"], "state": "running", "session_id": "fixture-native"}

        monkeypatch.setattr(studio.harnesses, "library", library)
        monkeypatch.setattr(studio.harnesses, "launch", launch)
        app = create_studio_gateway(control)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:7777",
            headers={"X-World-Control-Token": "operator-test-only"},
        ) as client:
            yield world, studio, client, requests, launches, connection
        await world.aclose()


def run_body(**changes):
    return {
        "harness_id": "codex",
        "auth_mode": "api_key",
        "model": "studio-model",
        "budget_usd": 1,
        "wall_minutes": 10,
        **changes,
    }


async def test_settings_are_atomic_private_and_never_echo_submitted_secrets(product):
    world, _studio, client, requests, _launches, _connection = product
    key = "fixture-provider-key-not-for-production"
    saved = await client.post("/api/settings", json={"credentials": {"openai_api_key": key}})
    assert saved.status_code == 200 and saved.json()["credentials"]["openai"]["configured"]
    assert key not in saved.text
    assert not requests
    encrypted = world.store.data_dir / "instance-settings/state.enc"
    assert key.encode() not in encrypted.read_bytes()
    revision = saved.json()["revision"]
    rejected = await client.post(
        "/api/settings",
        json={
            "credentials": {"openai_api_key": "secret-must-not-echo"},
            "settings": {"arbitrary": key},
        },
    )
    assert rejected.status_code == 400 and key not in rejected.text
    assert world.instance_settings.status()["revision"] == revision
    assert world.credential("WORLD_OPENAI_API_KEY") == key
    assert (await client.delete("/api/settings/providers/openai/key")).status_code == 200
    assert world.credential("WORLD_OPENAI_API_KEY") is None


async def test_library_can_finish_setup_with_subscription_and_no_api_key(product):
    _world, _studio, client, requests, _launches, connection = product
    before = (await client.get("/api/settings")).json()
    assert before["setup_complete"] is False
    connection.update(state="connected", auth_method="subscription")
    after = (await client.get("/api/settings")).json()
    assert after["setup_complete"] is True
    assert not after["credentials"]["openai"]["configured"]
    item = next(
        row
        for row in (await client.get("/api/harnesses")).json()["harnesses"]
        if row["id"] == "codex"
    )
    assert {row["id"]: row["ready"] for row in item["connections"]} == {
        "api_key": False,
        "subscription": True,
    }
    subscription = next(row for row in item["connections"] if row["id"] == "subscription")
    # An account is offered exact models only; no alias or "account default".
    assert [row["id"] for row in subscription["models"]] == ["studio-model"]
    assert subscription["billing"]["kind"] == "subscription"
    assert not requests


async def test_one_click_api_run_registers_actual_conditions_and_hides_capability(product):
    world, _studio, client, requests, launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    response = await client.post("/api/runs", json=run_body(name="First expedition", goal=None))
    assert response.status_code == 201, response.text
    info, harness, auth_mode, model = launches[0]
    config = world.run_config(info["run_id"])
    assert config["name"] == "First expedition" and config["goal"] is None
    assert config["stage"] == 2 and config["research_mode"] == "development"
    assert config["operator_messages_allowed"] is False
    assert config["model_billing_mode"] == "api_key"
    assert (harness, auth_mode, model) == ("codex", "api_key", "studio-model")
    assert info["run_token"] not in response.text and "run_token" not in response.text
    assert not requests


async def test_subscription_run_has_truthful_briefing_and_unmeasured_model_cost(product):
    world, _studio, client, requests, launches, connection = product
    connection.update(state="connected", auth_method="subscription")
    refused = await client.post("/api/runs", json=run_body(auth_mode="subscription", model=None))
    assert refused.status_code == 400 and not launches
    response = await client.post("/api/runs", json=run_body(auth_mode="subscription"))
    assert response.status_code == 201, response.text
    info = launches[0][0]
    run_id = info["run_id"]
    assert "model-dollar accounting are unavailable" in info["preamble"]
    assert "world-tool spend" in info["preamble"]
    assert world.store.get_run(run_id)["tainted"]
    billing = response.json()["billing"]
    assert billing["model_cost_usd"] is None and billing["cap_scope"] == "world_tools"
    rejected = await client.post(
        "/model/openai/v1/chat/completions",
        json={"model": "studio-model", "messages": []},
        headers={"Authorization": "Bearer " + info["run_token"], "X-World-Run-Id": run_id},
    )
    assert rejected.status_code == 403 and not requests
    await client.post(f"/api/runs/{run_id}/stop", json={})
    report = world.report(run_id)
    assert report["billing"]["model_usd"] is None
    assert report["billing"]["scientific_eligible"] is False
    from analysis.report import render_report

    assert "Subscription model dollars are unmeasured" in render_report(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"harness_id": "arbitrary"},
        {"model": "unpriced"},
        {"reply_policy": "blockers_plus_checkins"},
        {"packs": ["browse"]},
        {"command": "arbitrary"},
    ],
)
async def test_invalid_or_unready_launches_do_not_create_world_runs(product, changes):
    world, _studio, client, requests, launches, _connection = product
    count = len(world.store.list_runs())
    response = await client.post("/api/runs", json=run_body(**changes))
    assert response.status_code == 400
    assert len(world.store.list_runs()) == count and not launches and not requests


async def test_launcher_failure_seals_created_run_as_attrition(product, monkeypatch):
    world, studio, client, _requests, _launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")

    async def fail(*args, **kwargs):
        raise ValueError("Native fixture launch failed")

    monkeypatch.setattr(studio.harnesses, "launch", fail)
    response = await client.post("/api/runs", json=run_body())
    assert response.status_code == 400
    row = world.store.list_runs()[0]
    assert row["state"] == "sealed" and row["end"]["reason"] == "harness_launch_failed"
    assert world.store.verify_record(row["run_id"])["valid"]


async def test_stop_drains_native_process_before_seal_and_panels_are_passive(product, monkeypatch):
    world, studio, client, _requests, launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    response = await client.post("/api/runs", json=run_body())
    assert response.status_code == 201
    run_id = launches[0][0]["run_id"]
    before = world.store.records(run_id)
    for suffix in ("", "/beliefs", "/footprint", "/asks", "/events", "/ledger"):
        viewed = await client.get(f"/api/runs/{run_id}{suffix}")
        assert viewed.status_code == 200, viewed.text
    assert world.store.records(run_id) == before

    async def drained(selected):
        world.store.append_record(selected, "native_fixture_drained")

    monkeypatch.setattr(studio.harnesses, "stop_for_run", drained)
    stopped = await client.post(f"/api/runs/{run_id}/stop", json={})
    assert stopped.status_code == 200
    records = world.store.records(run_id)
    assert (
        next(row["seq"] for row in records if row["kind"] == "native_fixture_drained")
        < records[-1]["seq"]
    )
    assert records[-1]["kind"] == "seal" and world.store.verify_record(run_id)["valid"]


async def test_live_key_updates_do_not_reprice_an_existing_run(product):
    world, _studio, client, requests, _launches, _connection = product
    first_key, second_key = "fixture-initial-api-key", "fixture-replacement-api-key"
    world.instance_settings.update_provider("openai", api_key=first_key)
    first = world.create_run(RunConfig())
    world.instance_settings.update_provider(
        "openai", api_key=second_key, prices={"studio-model": price("2")}
    )
    second = world.create_run(RunConfig())
    assert first["tuple"]["price_table_id"] != second["tuple"]["price_table_id"]
    for info in (first, second):
        result = await client.post(
            "/model/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + info["run_token"],
                "X-World-Run-Id": info["run_id"],
            },
            json={"model": "studio-model", "messages": [], "max_tokens": 10},
        )
        assert result.status_code == 200, result.text
    assert all(row.headers["authorization"] == "Bearer " + second_key for row in requests)
    assert world.store.spend(first["run_id"])["model"] == pytest.approx(0.0002)
    assert world.store.spend(second["run_id"])["model"] == pytest.approx(0.0004)


@pytest.mark.parametrize(
    "change",
    [
        {"research_mode": "research"},
        {"isolation_profile": "fenced"},
        {"life_id": "life_fake"},
        {"series_id": "series_fake"},
        {"measurement_of": "r_fake"},
    ],
)
def test_subscription_cannot_claim_research_or_measurement_conditions(change):
    with pytest.raises(ValueError, match="subscription"):
        RunConfig(model_billing_mode="subscription", **change)


def life_body(**changes):
    return {
        "harness_id": "codex",
        "auth_mode": "api_key",
        "model": "studio-model",
        "balance_usd": 2,
        "session_minutes": 10,
        **changes,
    }


async def until(predicate):
    import asyncio

    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_a_life_lives_on_its_balance_with_no_objective_and_holds_its_harness(product):
    world, _studio, client, requests, launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    response = await client.post("/api/lives", json=life_body(name="First life"))
    assert response.status_code == 201, response.text
    life_id = response.json()["life"]["life_id"]
    await until(lambda: launches)
    info, harness, auth_mode, model = launches[0]
    config = world.run_config(info["run_id"])
    assert config["life_id"] == life_id and config["goal"] is None
    assert "schedule" in config["packs"] and "objective" not in config["packs"]
    assert "bounty" in config["packs"] and "market" not in config["packs"]
    assert world.store.reconcile(info["run_id"])["cap_usd"] == 2
    assert "Your balance starts at $2" in config["preamble"]
    assert "you rest until 24 hours pass" in config["preamble"]
    life = world.services["lives"].get(life_id)
    assert (life["on_exhaustion"], life["on_harness_exit"], life["ends_at"]) == (
        "strand",
        "rest",
        None,
    )
    assert (harness, auth_mode, model) == ("codex", "api_key", "studio-model")
    listed = (await client.get("/api/lives")).json()["lives"]
    assert [row["life_id"] for row in listed] == [life_id]
    assert listed[0]["name"] == "First life" and listed[0]["life_state"] == "awake"
    assert listed[0]["latest_run_id"] == info["run_id"]
    asks = await client.get(f"/api/lives/{life_id}/asks")
    assert asks.status_code == 200 and asks.json() == {"tickets": []}
    held = await client.post("/api/runs", json=run_body())
    assert held.status_code == 400 and "living life" in held.json()["detail"]
    assert not requests


async def test_a_resting_life_wakes_on_a_recorded_deposit_and_beam_up_ends_it(product):
    world, _studio, client, _requests, launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    life_id = (await client.post("/api/lives", json=life_body())).json()["life"]["life_id"]
    await until(lambda: launches)
    await world.end_run(launches[0][0]["run_id"], reason="harness_stopped")
    await until(lambda: world.services["lives"].status(life_id)["life_state"] == "resting")
    deposited = await client.post(
        f"/api/lives/{life_id}/deposits",
        json={
            "usd": 1,
            "source": "Client paid invoice 3",
            "evidence": "Statement line 12",
            "request_id": "deposit-0001",
        },
    )
    assert deposited.status_code == 201, deposited.text
    account = deposited.json()["account"]
    assert account["income_by_channel"] == {"recorded": 1}
    await until(lambda: len(launches) == 2)
    inbox = world.store.inbox(launches[1][0]["run_id"])["events"]
    assert any(event["type"] == "deposit" for event in inbox)
    ended = await client.post(f"/api/lives/{life_id}/beam-up", json={})
    assert ended.json()["life"]["ended"]["reason"] == "beam_up"
    await until(
        lambda: (
            not world.services["studies"].list_life_runners(life_id)[-1]["state"]
            in {"running", "waiting"}
        )
    )
    assert (await client.post("/api/runs", json=run_body())).status_code == 201


async def test_an_objective_is_optional_and_becomes_the_session_goal(product):
    world, _studio, client, _requests, launches, _connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    body = life_body(objective="  Publish one useful dataset.  ", market=True)
    assert (await client.post("/api/lives", json=body)).status_code == 201
    await until(lambda: launches)
    config = world.run_config(launches[0][0]["run_id"])
    assert config["goal"] == "Publish one useful dataset."
    assert {"objective", "market"} <= set(config["packs"])
    assert config["market_settlement"] == "balance"


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_mode": "subscription", "model": None},
        {"harness_id": "arbitrary"},
        {"packs": ["browse"]},
        {"goal": "A run field"},
    ],
)
async def test_unsupported_lives_create_nothing(product, changes):
    world, _studio, client, _requests, launches, connection = product
    world.instance_settings.update_provider("openai", api_key="fixture-openai-key")
    connection.update(state="connected", auth_method="subscription")
    response = await client.post("/api/lives", json=life_body(**changes))
    assert response.status_code == 400
    assert not world.services["lives"].list() and not launches


async def test_bounties_are_posted_and_listed(product):
    _world, _studio, client, _requests, _launches, _connection = product
    posted = await client.post(
        "/api/bounties", json={"title": "Fix a dead link", "criterion": "Merged", "usd": 3}
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["bounties"][0]["title"] == "Fix a dead link"


async def test_a_claude_code_subscription_life_lives_on_an_estimated_balance(product):
    world, _studio, client, requests, launches, connection = product
    connection.update(state="connected", auth_method="subscription")
    body = life_body(harness_id="claude-code", auth_mode="subscription", model="claude-studio")
    response = await client.post("/api/lives", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["life"]["estimated"] is True
    await until(lambda: launches)
    info, harness, auth_mode, model = launches[0]
    assert (harness, auth_mode, model) == ("claude-code", "subscription", "claude-studio")
    config = world.run_config(info["run_id"])
    assert config["model_metering"] == "transcript"
    assert config["model_billing_mode"] == "subscription"
    assert "estimated from your session's own usage reports" in config["preamble"]
    billing = (await client.get(f"/api/runs/{info['run_id']}")).json()["billing"]
    assert billing["model_cost_estimated"] is True and billing["model_cost_usd"] is None
    assert not requests


async def test_a_held_life_switches_model_and_resumes_on_a_fresh_runner(product):
    world, _studio, client, _requests, launches, connection = product
    lives, studies = world.services["lives"], world.services["studies"]
    connection.update(state="connected", auth_method="subscription")
    body = life_body(harness_id="claude-code", auth_mode="subscription", model="claude-studio")
    life_id = (await client.post("/api/lives", json=body)).json()["life"]["life_id"]
    await until(lambda: launches)
    run_id = launches[0][0]["run_id"]
    world.store.append_record(
        run_id,
        "model_error",
        status=404,
        error="model_not_found",
        error_class="model_unavailable",
        model="claude-studio",
        message="There's an issue with the selected model",
    )
    await world.end_run(run_id, reason="model_error")
    await until(lambda: lives.get(life_id)["state"] == "model_hold")
    held = (await client.get(f"/api/lives/{life_id}")).json()["life"]
    assert held["life_state"] == "held" and held["hold_reason"]["status"] == 404
    for refused in ("claude-studio", "studio-model", "claude-unknown"):
        response = await client.post(f"/api/lives/{life_id}/model", json={"model": refused})
        assert response.status_code == 400, refused
    response = await client.post(
        f"/api/lives/{life_id}/model", json={"model": "claude-studio-next"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["life"]["model"] == "claude-studio-next"
    await until(lambda: len(launches) == 2)
    assert launches[1][1:] == ("claude-code", "subscription", "claude-studio-next")
    assert world.run_config(launches[1][0]["run_id"])["model_snapshot_id"] == "claude-studio-next"
    first, second = studies.list_life_runners(life_id)
    assert first["state"] == "completed" and first["reason"] == "model_switched"
    assert second["state"] == "running"
    switched = [e for e in lives.events(life_id) if e["kind"] == "model_switched"]
    assert switched[0]["model_from"] == "claude-studio"
    assert switched[0]["model_to"] == "claude-studio-next"
    response = await client.post(f"/api/lives/{life_id}/model", json={"model": "claude-studio"})
    assert response.status_code == 400
    await client.post(f"/api/lives/{life_id}/beam-up")


def effortful(studio, monkeypatch):
    catalog = {
        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "models": {
            "claude-studio": {
                "label": "Claude Studio",
                "group": "current",
                "efforts": ["low", "medium", "high"],
                "default_effort": "medium",
            },
            "claude-studio-next": {
                "label": "Claude Studio Next",
                "group": "earlier",
                "efforts": [],
            },
        },
    }
    monkeypatch.setattr(studio, "model_catalog", catalog)


async def test_each_connection_offers_what_it_serves_crossed_with_reviewed_prices(
    product, monkeypatch
):
    world, studio, client, _requests, _launches, connection = product
    effortful(studio, monkeypatch)
    world.instance_settings.update_provider("anthropic", api_key="fixture-anthropic-key")
    world.instance_settings._save_served(
        "anthropic", ["claude-studio", "claude-unreviewed"], "2026-10-24T00:00:00Z"
    )
    connection.update(state="connected", auth_method="subscription")
    item = next(
        row
        for row in (await client.get("/api/harnesses")).json()["harnesses"]
        if row["id"] == "claude-code"
    )
    assert (item["kind"], item["provider"], item["transcript"]) == (
        "native",
        "anthropic",
        "claude-stream-json",
    )
    key, account = item["connections"]
    assert key["label"] == "Anthropic API key" and key["billing"]["kind"] == "metered"
    rows = {row["id"]: row for row in key["models"]}
    assert rows["claude-studio"]["served"] is True and rows["claude-studio"]["selectable"]
    assert rows["claude-studio"]["label"] == "Claude Studio"
    assert rows["claude-studio"]["efforts"] == ["low", "medium", "high"]
    # The key does not serve it, so it cannot be chosen; nor can an unpriced one it does.
    assert rows["claude-studio-next"]["served"] is False
    assert not rows["claude-studio-next"]["selectable"]
    assert not rows["claude-unreviewed"]["priced"] and not rows["claude-unreviewed"]["selectable"]
    assert account["billing"]["kind"] == "estimated"
    assert {row["id"] for row in account["models"]} == {"claude-studio", "claude-studio-next"}
    assert all(row["served"] is None for row in account["models"])


async def test_effort_is_a_registered_condition_passed_as_chosen(product, monkeypatch):
    world, studio, client, _requests, launches, connection = product
    effortful(studio, monkeypatch)
    connection.update(state="connected", auth_method="subscription")
    body = run_body(harness_id="claude-code", auth_mode="subscription", model="claude-studio")
    for refused in (
        {**body, "effort": "max"},
        {**body, "model": "claude-studio-next", "effort": "low"},
    ):
        response = await client.post("/api/runs", json=refused)
        assert response.status_code == 400, refused
    chosen = await client.post("/api/runs", json={**body, "effort": "high"})
    assert chosen.status_code == 201, chosen.text
    config = world.run_config(launches[-1][0]["run_id"])
    assert config["effort"] == "high" and config["model_metering"] == "transcript"
    await client.post(f"/api/runs/{launches[-1][0]['run_id']}/stop", json={})
    # Left to the model's default, the default is still recorded explicitly.
    default = await client.post("/api/runs", json=body)
    assert default.status_code == 201, default.text
    second = world.run_config(launches[-1][0]["run_id"])
    assert second["effort"] == "medium"
    assert (
        world.store.get_run(launches[-1][0]["run_id"])["config"]["tuple"]["runtime_protocol_id"]
        != world.store.get_run(launches[-2][0]["run_id"])["config"]["tuple"]["runtime_protocol_id"]
    )
    await client.post(f"/api/runs/{launches[-1][0]['run_id']}/stop", json={})
    none = await client.post("/api/runs", json={**body, "model": "claude-studio-next"})
    assert none.status_code == 201, none.text
    assert world.run_config(launches[-1][0]["run_id"])["effort"] is None


async def test_a_codex_account_lists_its_own_catalogue(product, monkeypatch):
    _world, studio, client, _requests, _launches, connection = product
    connection.update(state="connected", auth_method="subscription")
    served = [
        {"id": "studio-model", "efforts": ["low", "medium", "ultra"], "default_effort": "low"},
        {"id": "gpt-unreviewed", "efforts": ["low"], "default_effort": "low"},
    ]
    auth = studio.harnesses._auth()
    monkeypatch.setattr(auth, "served_models", lambda harness_id, instance_id="default": served)
    item = next(
        row
        for row in (await client.get("/api/harnesses")).json()["harnesses"]
        if row["id"] == "codex"
    )
    account = next(row for row in item["connections"] if row["id"] == "subscription")
    rows = {row["id"]: row for row in account["models"]}
    assert rows["studio-model"]["efforts"] == ["low", "medium", "ultra"]
    assert rows["studio-model"]["default_effort"] == "low"
    # Codex's account spend is unmeasured, so an unpriced model stays selectable.
    assert rows["gpt-unreviewed"]["selectable"] and not rows["gpt-unreviewed"]["priced"]


@pytest.mark.parametrize(
    "changes",
    [
        {"harness_id": "codex", "auth_mode": "subscription", "model": "studio-model"},
        {"harness_id": "claude-code", "auth_mode": "subscription", "model": "sonnet"},
        {"harness_id": "claude-code", "auth_mode": "subscription", "model": None},
    ],
)
async def test_subscription_lives_need_claude_code_and_an_exact_priced_model(product, changes):
    world, _studio, client, _requests, launches, connection = product
    connection.update(state="connected", auth_method="subscription")
    response = await client.post("/api/lives", json=life_body(**changes))
    assert response.status_code == 400
    assert not world.services["lives"].list() and not launches
