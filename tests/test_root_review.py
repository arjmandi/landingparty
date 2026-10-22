"""Cross-service regressions found by independent review of the root integration."""

import json
from pathlib import Path

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig


@pytest.fixture
async def reviewed_world(tmp_path):
    requests = []

    async def respond(request):
        requests.append((request.method, str(request.url)))
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "fixture-reflection",
                    "choices": [{"message": {"content": '{"operations":[]}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200, text="<h1>Fixture evidence</h1>", headers={"content-type": "text/html"}
        )

    async def resolve(host, port):
        return ["93.184.216.34"]

    prices = {
        "models": {
            "fixture-reflection": {
                "provider": "openai",
                "input": "1",
                "output": "1",
                "cache_read": "1",
                "context_window": 100,
                "max_output_tokens": 4096,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client=upstream, resolver=resolve)
        app = create_app(tmp_path, gateway=gateway, lane_client=upstream, prices=prices)
        try:
            yield app.state.world, requests
        finally:
            await app.state.world.aclose()


async def test_actual_asgi_reflection_callback_reconciles_each_settled_call(reviewed_world):
    world, requests = reviewed_world
    run_id = world.create_run(
        RunConfig(
            stage=1,
            packs=["belief", "reflect"],
            reflection_policy="self",
            reflection_model="fixture-reflection",
            reflection_structuring_model="fixture-reflection",
        )
    )["run_id"]
    # This uses configure_models' real internal ASGI client and the real lane;
    # only the remote provider transport is replaced by a deterministic fixture.
    result = await world.services["reflection"].trigger(
        run_id, {"type": "belief_review", "event_id": "review_fixture"}
    )
    assert result["kind"] == "result", result
    assert result["operations"] == []
    assert len(requests) == 2
    records = world.store.records(run_id)
    calls = [record for record in records if record["kind"] == "model_call"]
    assert len(calls) == 2
    for call in calls:
        debits = [
            record
            for record in records
            if record["kind"] == "ledger_debit"
            and record["reservation_id"] == call["reservation_id"]
        ]
        assert len(debits) == 1 and debits[0]["cost_usd"] == call["usd"]
    assert world.store.reconcile(run_id)["valid"]


async def test_raw_network_admission_respects_unresolved_life_billing(reviewed_world):
    world, requests = reviewed_world
    lives = world.services["lives"]
    life_id = lives.create({"stage": 2, "budget": {"usd_life": 20}})["life_id"]
    created = await lives.start_session(
        life_id,
        {"raw_lane_enabled": True, "human_time_priced": "in_budget", "human_rate_per_minute": 1},
    )
    run_id = created["run_id"]
    hold = lives.reserve_human(life_id, 1, evidence_ref="review:uncertain", source_run_id=run_id)
    lives._freeze_billing(hold)
    ordinary = await world.act(run_id, "fetch", {"url": "https://fixture.test/evidence"})
    raw = await world.submit_network(run_id, "GET", "https://fixture.test/evidence")
    assert ordinary["rule_id"] == "life_billing_hold"
    assert raw["kind"] == "blocked" and raw["rule_id"] == "life_billing_hold"
    assert requests == []


async def test_operator_checkin_charges_registered_in_budget_human_time(reviewed_world):
    world, _ = reviewed_world
    run_id = world.create_run(
        RunConfig(
            human_time_priced="in_budget",
            human_rate_per_minute=1,
            reply_policy="blockers_plus_checkins",
            operator_messages_allowed=True,
        )
    )["run_id"]
    event = world.operator_message(run_id, "A researcher check-in", human_seconds=60)
    assert event["type"] == "operator_message"
    assert world.store.spend(run_id)["human"] == 1
    assert world.store.reconcile(run_id)["valid"]


async def test_unrelated_belief_import_uses_pattern_transfer_not_life_continuity(reviewed_world):
    world, _ = reviewed_world
    donor = world.create_run(RunConfig(stage=1, packs=["belief"]))["run_id"]
    fetched = await world.act(donor, "fetch", {"url": "https://fixture.test/evidence"})
    proposal = await world.act(
        donor,
        "believe",
        {
            "record": {
                "kind": "fact",
                "claim": "A tentative fixture fact",
                "rank": 2,
                "triple": {"subject": "fixture", "predicate": "status", "object": "tentative"},
                "evidence": [{"ref": Path(fetched["handle"]).stem, "class": "observed"}],
            }
        },
    )
    assert proposal["kind"] == "result", proposal
    belief_id = proposal["data"]["id"]
    await world.end_run(donor)
    assert belief_id not in world.services["beliefs"].export_bundle(donor)["manifest"]["belief_ids"]
    recipient = world.create_run(
        RunConfig(stage=1, packs=["belief"], iota={"beliefs": f"import({donor})"})
    )["run_id"]
    assert not any(
        record["id"] == belief_id for record in world.services["beliefs"].fold(recipient)
    )


async def test_accepted_unfinished_ticket_is_counted_at_episode_end(reviewed_world):
    world, _ = reviewed_world
    run_id = world.create_run(RunConfig(stage=2, reply_policy="blockers_only"))["run_id"]
    asks = world.services["asks"]
    ticket = asks.raise_ticket(
        run_id, "a_missing_credential", "grant_credential", pending_action={"alias": "fixture"}
    )
    assert asks.answer(ticket["ticket_id"], "deposit")["state"] == "accepted"
    ended = await world.end_run(run_id)
    assert ended["end"]["open_tickets_at_end"] == 1


async def test_shuffled_control_uses_only_new_links_and_one_public_history_row(reviewed_world):
    from worldd.belief_controls import prepare_control

    world, _ = reviewed_world
    service = world.services["beliefs"]
    donor = world.create_run(RunConfig(stage=1, packs=["belief", "deliver"]))["run_id"]
    evidence = await world.act(donor, "deliver", {"kind": "note", "body": "Source control fixture"})
    ids = []
    for index, rank in enumerate((3, 5)):
        result = await world.act(
            donor,
            "believe",
            {
                "record": {
                    "kind": "rule",
                    "claim": f"Review controlled strategy {index} carefully.",
                    "rank": rank,
                    "evidence": [{"ref": evidence["action_id"], "class": "observed"}],
                }
            },
        )
        ids.append(result["data"]["id"])
    assert (
        await world.act(donor, "link", {"from": ids[0], "to": ids[1], "relation": "contradicts"})
    )["kind"] == "result"
    await world.end_run(donor)
    original = service.export_bundle(donor)
    by_id = {row["id"]: row for row in original["projection"]}
    seed = next(
        seed
        for seed in range(64)
        if any(
            row["links"] != by_id[row["id"]]["links"]
            for row in prepare_control(original, seed)["projection"]
        )
    )
    child = world.create_run(
        RunConfig(
            stage=1,
            packs=["belief"],
            seed=seed,
            belief_import_policy="shuffled",
            iota={"beliefs": f"import({donor})"},
        )
    )["run_id"]
    current = service.fold(child)
    for row in current:
        assert row["n_contra"] == sum(edge["relation"] == "contradicts" for edge in row["links"])
        history = service.query(child, query="history", id=row["id"])
        assert len(history) == 1 and history[0]["rank"] == row["rank"]
        assert "initial_condition" not in json.dumps(
            history + [service.query(child, query="why", id=row["id"])]
        )
    for file in service.beliefs(child).views_dir.iterdir():
        assert "world_experimental_initialization" not in file.read_text()
        assert "source_projection_hash" not in file.read_text()
    private = world.store.run_dir(child) / "belief-controls/authenticated-donor.json"
    assert service.validate_bundle(json.loads(private.read_text()))["valid"]
    assert service.fold(donor) == list(by_id.values())


async def test_judge_and_public_review_routes_exist_only_on_trusted_control(tmp_path, monkeypatch):
    from worldd.public_identity import create_public_app
    from worldd.security import create_agent_app

    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "review-control-authority")
    app = create_app(tmp_path)
    world = app.state.world
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://control"
            ) as control,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(create_agent_app(world)), base_url="http://agent"
            ) as agent,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(create_public_app(world)), base_url="http://public"
            ) as public,
        ):
            headers = {"X-World-Control-Token": "review-control-authority"}
            for path in ("/v1/judge-canaries", "/v1/public-incident-reports"):
                assert (await control.get(path)).status_code == 401
                assert (await agent.get(path)).status_code == 404
                assert (await public.get(path)).status_code == 404
                assert (await control.get(path, headers=headers)).status_code == 200
            assert (await control.post("/v1/judge-canaries/tick")).status_code == 401
            assert (await agent.post("/v1/judge-canaries/tick")).status_code == 404
            assert (await public.post("/v1/judge-canaries/tick")).status_code == 404
            assert (
                await control.post("/v1/judge-canaries/tick", headers=headers)
            ).status_code == 200
            malformed = await control.post(
                "/v1/judge-canaries",
                headers=headers,
                json={
                    "source_run_id": "missing",
                    "config": {"callback": "os.system", "method_name": "__import__"},
                },
            )
            assert malformed.status_code in {400, 404}
            assert world.services["judge_canaries"].list() == []
    finally:
        await world.aclose()


async def test_shuffle_rebuilds_only_graph_feedback_and_preserves_direct_counterevidence(
    reviewed_world,
):
    from worldd.belief_controls import prepare_control

    world, _ = reviewed_world
    service = world.services["beliefs"]
    donor = world.create_run(RunConfig(stage=1, packs=["belief", "deliver"]))["run_id"]
    evidence = await world.act(
        donor, "deliver", {"kind": "note", "body": "Counterevidence fixture"}
    )
    fact = (
        await world.act(
            donor,
            "believe",
            {
                "record": {
                    "kind": "fact",
                    "claim": "Fixture harbour remains open.",
                    "rank": 4,
                    "triple": {
                        "subject": "fixture harbour",
                        "predicate": "status",
                        "object": "open",
                    },
                    "evidence": [{"ref": evidence["action_id"], "class": "observed"}],
                }
            },
        )
    )["data"]["id"]
    dependent = (
        await world.act(
            donor,
            "believe",
            {
                "record": {
                    "kind": "strategy",
                    "claim": "Visit the fixture harbour tomorrow.",
                    "rank": 3,
                    "links": [{"to": fact, "relation": "derived_from"}],
                }
            },
        )
    )["data"]["id"]
    await world.act(
        donor,
        "believe",
        {
            "record": {
                "kind": "strategy",
                "claim": "Use another fixture source instead.",
                "rank": 5,
                "evidence": [{"ref": evidence["action_id"], "class": "observed"}],
            }
        },
    )
    await service.observe(
        donor,
        {
            "observation_ref": "review_actual_counterevidence",
            "triple": {"subject": "fixture harbour", "predicate": "status", "object": "closed"},
        },
    )
    await world.end_run(donor)
    bundle = service.export_bundle(donor)
    before = json.dumps(bundle, sort_keys=True)
    original = {row["id"]: row for row in bundle["projection"]}
    assert original[dependent]["kernel"]["support_lost"]
    controls = (prepare_control(bundle, seed) for seed in range(64))
    control = next(
        control
        for control in controls
        if any(row["links"] != original[row["id"]]["links"] for row in control["projection"])
    )
    for row in control["projection"]:
        for lost in row["kernel"]["support_lost"]:
            assert {"to": lost["from"], "relation": "derived_from"} in row["links"]
            assert lost["observation_ref"] == "review_actual_counterevidence"
        if row["id"] == fact:
            assert row["n_contra"] == original[fact]["n_contra"] == 1
            assert row["kernel"]["evidence"] == original[fact]["kernel"]["evidence"]
            assert row["kernel"]["classifications"] == original[fact]["kernel"]["classifications"]
    assert json.dumps(bundle, sort_keys=True) == before
