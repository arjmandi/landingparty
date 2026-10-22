"""Cross-service regressions found by independent review of the root integration."""

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
