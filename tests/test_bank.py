"""One money account per life: deposits, market dollars, bounties, and waking on income."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from worldd.app import create_app
from worldd.run_views import RunViews


@pytest.fixture
async def world(tmp_path):
    app = create_app(tmp_path)
    yield app.state.world
    await app.state.world.aclose()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def bank_life(world, usd=1, *, channels=("recorded", "bounty", "market"), **extra):
    return world.services["lives"].create(
        {
            "stage": 2,
            "budget": {"usd_life": usd, "wall_life_days": None},
            "driver": {"metric": "usd_balance"},
            "income_channels": list(channels),
            "market_settlement": "balance" if "market" in channels else "paper",
            "on_exhaustion": "strand",
            "on_harness_exit": "rest",
            **extra,
        }
    )["life_id"]


async def session(world, life_id, packs=("schedule", "market")):
    created = await world.services["lives"].start_session(
        life_id, {"packs": list(packs), "budget": {"usd": 10000, "wall_s": 60}}
    )
    return created["run_id"]


async def test_recorded_deposit_is_idempotent_and_announced_once(world):
    bank = world.services["bank"]
    life_id = bank_life(world)
    run_id = await session(world, life_id)
    first = bank.record(
        life_id, usd=2, source="Invoice 7 paid", evidence="Stripe po_1", request_id="request-0001"
    )
    again = bank.record(
        life_id, usd=2, source="Invoice 7 paid", evidence="Stripe po_1", request_id="request-0001"
    )
    assert (first["applied"], again["applied"]) == (True, False)
    assert again["balance_usd"] == 3
    events = [e for e in world.store.inbox(run_id)["events"] if e["type"] == "deposit"]
    assert len(events) == 1
    assert (
        events[0]["channel"] == "recorded" and events[0]["memo"] == "Invoice 7 paid — Stripe po_1"
    )
    inflow = [m for m in again["movements"] if m["direction"] == "in"]
    assert inflow[0]["author_class"] == "human_attested"


async def test_a_channel_the_life_did_not_declare_is_refused(world):
    bank = world.services["bank"]
    life_id = bank_life(world, channels=("bounty",))
    with pytest.raises(ValueError, match="declared"):
        bank.record(life_id, usd=1, source="A gift", evidence="none", request_id="request-0002")
    with pytest.raises(ValueError, match="provenance"):
        bank.deposit(life_id, 1, "bounty", evidence_ref="bounty:x", author_class="human_attested")


async def test_a_deposit_wakes_a_stranded_life_and_reaches_the_next_session(world):
    bank, lives = world.services["bank"], world.services["lives"]
    life_id = bank_life(world)
    run_id = await session(world, life_id)
    assert world.store.debit(run_id, "model", 1)
    await world.end_run(run_id, reason="harness_stopped")
    assert lives.get(life_id)["state"] == "stranded"
    bank.record(
        life_id,
        usd="0.5",
        source="Consulting call",
        evidence="Bank line 4",
        request_id="request-0003",
    )
    assert lives.get(life_id)["state"] == "waking"
    following = await session(world, life_id)
    types = [event["type"] for event in world.store.inbox(following)["events"]]
    assert "deposit" in types and "wake" in types
    assert world.store.reconcile(following)["cap_usd"] == 0.5


def market(world, market_id="m_rain", price="0.25"):
    service = world.services["market"]
    service.register_market(
        market_id,
        price=price,
        source_url="https://example.com/question",
        closes_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    return service


def evidence(world, run_id):
    world.store.append_record(
        run_id,
        "action",
        action_id="a_fetch",
        tool="fetch",
        params={"url": "https://example.com/question"},
    )
    world.store.append_record(run_id, "outcome", action_id="a_fetch", reply={"kind": "result"})
    return "a_fetch"


async def test_a_market_stake_spends_dollars_and_a_win_pays_them_back(world):
    lives = world.services["lives"]
    life_id = bank_life(world, usd=2)
    run_id = await session(world, life_id)
    service = market(world)
    reply = service.trade(
        run_id,
        "a_trade",
        market_id="m_rain",
        side="yes",
        credits="0.5",
        evidence_ref=evidence(world, run_id),
    )
    assert reply["kind"] == "result", reply
    assert world.store.spend(run_id)["market"] == 0.5
    assert lives.status(life_id)["balance_usd"] == 1.5
    costs = RunViews(world).get(run_id)["costs"]
    assert (costs["market_stakes_usd"], costs["world_tools_usd"]) == (0.5, 0)
    duplicate = service.trade(
        run_id, "a_trade", market_id="m_rain", side="yes", credits="0.5", evidence_ref="a_fetch"
    )
    assert duplicate["fill"]["fill_id"] == reply["fill"]["fill_id"]
    assert world.store.spend(run_id)["market"] == 0.5
    service.resolve("m_rain", outcome=True, observation_ref="obs_resolution")
    await until(lambda: lives.status(life_id)["balance_usd"] == 3.5)
    assert world.services["bank"].account(life_id)["income_by_channel"] == {"market": 2}
    assert (await service.redeem(run_id, 1))["rule_id"] == "market_balance"


async def test_a_stake_the_balance_cannot_cover_is_blocked(world):
    life_id = bank_life(world, usd=1)
    run_id = await session(world, life_id)
    service = market(world)
    reply = service.trade(
        run_id,
        "a_trade",
        market_id="m_rain",
        side="no",
        credits="5",
        evidence_ref=evidence(world, run_id),
    )
    assert reply["rule_id"] == "market_balance"
    assert "market" not in world.store.spend(run_id)


async def test_an_approved_bounty_pays_even_while_the_life_is_stranded(world):
    lives, asks = world.services["lives"], world.services["asks"]
    life_id = bank_life(world)
    run_id = await session(world, life_id)
    service = world.services["market"]
    bounty = service.register_bounty(
        title="Fix one dead link",
        criterion="A merged fix",
        usd="3",
        sponsor="researcher",
        expires_at=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )
    ticket = await service.claim_bounty(run_id, "a_claim", bounty["bounty_id"], "a_evidence")
    assert world.store.debit(run_id, "model", 1)
    await world.end_run(run_id, reason="harness_stopped")
    assert lives.get(life_id)["state"] == "stranded"
    asks.answer(ticket["ticket_id"], "approve", ask_class="approval")
    assert lives.get(life_id)["state"] == "waking"
    assert world.services["bank"].account(life_id)["income_by_channel"] == {"bounty": 3}


async def test_a_recorded_deposit_without_evidence_is_refused(world):
    life_id = bank_life(world)
    with pytest.raises(ValueError, match="evidence"):
        world.services["bank"].record(
            life_id, usd=1, source="A transfer", evidence=" ", request_id="request-0009"
        )


async def test_a_dollar_life_trades_in_usd_while_paper_runs_keep_credits(world):
    from worldd.models import RunConfig, manifest

    life_id = bank_life(world, usd=2)
    run_id = await session(world, life_id)
    tools = {tool["name"]: tool for tool in world.manifest(run_id)["tools"]}
    assert set(tools["trade"]["inputSchema"]["properties"]) >= {"usd", "market_id"}
    assert "credits" not in tools["trade"]["inputSchema"]["properties"]
    paper = {tool["name"]: tool for tool in manifest(["market"])["tools"]}
    assert "credits" in paper["trade"]["inputSchema"]["properties"]
    assert (
        manifest(["market"])["tool_set_id"]
        == manifest(["market"], market_settlement="paper")["tool_set_id"]
    )
    assert RunConfig().market_settlement == "paper"
    market(world)
    reply = await world.act(
        run_id,
        "trade",
        {"market_id": "m_rain", "side": "yes", "usd": 0.5, "evidence_ref": evidence(world, run_id)},
    )
    assert reply["kind"] == "result", reply
    assert reply["fill"]["usd"] == "0.500000000" and "credits" not in reply["fill"]
    assert world.store.spend(run_id)["market"] == 0.5
    status = await world.act(run_id, "market_status", {})
    assert status["balance_usd"] == 1.5 and "cash_credits" not in str(status)
