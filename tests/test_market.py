from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from worldd.market import MarketService
from worldd.storage import Store


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.run_id = store.create_run(
        {"budget": {"usd": 20, "wall_s": 7200}, "life_id": "life_market"}
    )["run_id"]
    yield world
    store.close()


def market(world):
    service = MarketService(world)
    service.register_market(
        "election",
        price="0.5",
        source_url="https://source.example/outcome",
        closes_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    return service


def evidence(world):
    world.store.append_record(
        world.run_id,
        "action",
        action_id="a_fetch",
        tool="fetch",
        params={"url": "https://source.example/outcome"},
    )
    world.store.append_record(
        world.run_id, "outcome", action_id="a_fetch", reply={"kind": "result", "status": 200}
    )


async def test_trade_requires_actual_recent_matching_fetch(world):
    service = market(world)
    assert (
        service.trade(world.run_id, "a_trade", market_id="election", side="yes", credits=10)[
            "rule_id"
        ]
        == "trade_evidence"
    )
    evidence(world)
    fill = service.trade(world.run_id, "a_trade", market_id="election", side="yes", credits=10)
    assert fill["fill"]["quantity"] == "20.000000000"
    assert fill["fill"]["evidence_ref"] == "a_fetch"
    assert service.balance(world.run_id)["credits"] == 990
    assert (
        service.trade(world.run_id, "a_trade", market_id="election", side="yes", credits=10) == fill
    )
    assert service.balance(world.run_id)["credits"] == 990
    await service.aclose()


async def test_paper_balance_position_and_resolution_are_atomic(world):
    service = market(world)
    evidence(world)
    assert (
        service.trade(world.run_id, "a_too_much", market_id="election", side="yes", credits=1001)[
            "rule_id"
        ]
        == "paper_balance"
    )
    assert service.balance(world.run_id)["credits"] == 1000
    service.trade(world.run_id, "a_yes", market_id="election", side="yes", credits=100)
    resolved = service.resolve("election", outcome=True, observation_ref="world:outcome")
    assert resolved["payouts"][0]["credits"] == "200.000000000"
    assert service.balance(world.run_id)["credits"] == 1100
    service.resolve("election", outcome=True, observation_ref="world:outcome")
    assert service.balance(world.run_id)["credits"] == 1100
    with pytest.raises(ValueError):
        service.resolve("election", outcome=False, observation_ref="different:outcome")
    assert (
        service.trade(world.run_id, "a_late", market_id="election", side="yes", credits=1)[
            "rule_id"
        ]
        == "market_closed"
    )
    await service.aclose()


async def test_sell_cannot_create_unheld_position(world):
    service = market(world)
    evidence(world)
    assert (
        service.trade(world.run_id, "a_short", market_id="election", side="sell_yes", credits=1)[
            "kind"
        ]
        == "blocked"
    )
    service.trade(world.run_id, "a_buy", market_id="election", side="yes", credits=10)
    service.mark("election", price="0.6", observation_ref="world:price")
    sold = service.trade(world.run_id, "a_sell", market_id="election", side="sell_yes", credits=12)
    assert sold["kind"] == "result"
    assert service.balance(world.run_id)["credits"] == 1002
    await service.aclose()


async def test_income_only_redeems_net_profit_through_declared_rail(world):
    service = market(world)
    evidence(world)
    credits = []
    world.services["lives"] = SimpleNamespace(
        get=lambda life_id: {"income_rail": {"kind": "market", "usd_per_credit": "0.01"}},
        credit=lambda *args, **kwargs: credits.append((args, kwargs)),
    )
    assert (await service.redeem(world.run_id, 1))["rule_id"] == "paper_profit"
    service.trade(world.run_id, "a_yes", market_id="election", side="yes", credits=100)
    service.resolve("election", outcome=True, observation_ref="world:resolution")
    redeemed = await service.redeem(world.run_id, 50)
    assert redeemed["kind"] == "result"
    assert float(redeemed["usd"]) == 0.5
    assert service.balance(world.run_id)["credits"] == 1050
    assert credits[0][0][0] == "life_market" and credits[0][0][2] == "market"
    assert credits[0][1]["author_class"] == "sponsor"
    await service.recover()
    assert len(credits) == 1
    await service.aclose()


async def test_bounty_requires_sponsor_adjudication_and_evidence(world):
    service = MarketService(world)
    bounty = service.register_bounty(
        title="Artifact",
        criterion="Pass the declared audit",
        usd="2",
        sponsor="Researcher",
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    result = await service.claim_bounty(
        world.run_id, "a_claim", bounty["bounty_id"], "offered:artifact"
    )
    assert result["rule_id"] == "bounty_claim"
    listed = await service.handle(world.run_id, "a_list", "bounties", {})
    assert listed["items"][0]["sponsor"] == "Researcher"
    await service.aclose()


async def test_marked_equity_history_and_passive_tranches(world):
    service = market(world)
    evidence(world)
    service.trade(world.run_id, "a_buy", market_id="election", side="yes", credits=100)
    service.mark("election", price="0.6", observation_ref="world:mark")
    equity = service.equity(world.run_id)
    assert float(equity["equity_credits"]) == 1020
    assert float(equity["matched_entry_passive_tranches"][0]["pnl_credits"]) == 20
    service.trade(world.run_id, "a_sell", market_id="election", side="sell_yes", credits=120)
    assert float(service.equity(world.run_id)["equity_credits"]) == 1020
    assert service.db.execute("SELECT COUNT(*) FROM paper_equity").fetchone()[0] == 1
    service.resolve("election", outcome=False, observation_ref="world:outcome")
    assert (
        float(service.equity(world.run_id)["matched_entry_passive_tranches"][0]["pnl_credits"])
        == -100
    )
    await service.aclose()


async def test_independent_income_requires_signed_receipt_and_classified_buyer(world, monkeypatch):
    import hashlib
    import hmac
    import json
    import time

    from worldd.communications import CommunicationsService
    from worldd.footprint import FootprintService

    world.run_id = world.store.create_run(
        {
            "stage": 3,
            "research_mode": "research",
            "life_id": "independent_life",
            "budget": {"usd": 1, "wall_s": 300},
        }
    )["run_id"]
    service, communications, footprint = (
        MarketService(world),
        CommunicationsService(world),
        FootprintService(world),
    )
    monkeypatch.setenv("WORLD_INCOME_WEBHOOK_KEY", "income-fixture-key")
    service.register_income_webhook("store", "WORLD_INCOME_WEBHOOK_KEY")
    credits = []
    life = {"ended": None, "income_rail": {"kind": "independent"}}
    world.services.update(
        communications=communications,
        footprint=footprint,
        lives=SimpleNamespace(
            get=lambda life_id: life,
            status=lambda life_id: {"driver": {"reading": 1, "floor": 0}},
            credit=lambda *args, **kwargs: credits.append((args, kwargs)),
        ),
    )
    payload = {
        "paid": True,
        "currency": "USD",
        "external_id": "receipt1",
        "counterparty": "buyer1",
        "usd": "2.50",
        "author_class": "independent_human",
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    headers = {
        "x-landing-party-timestamp": timestamp,
        "x-landing-party-signature": hmac.new(
            b"income-fixture-key", timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest(),
    }
    with pytest.raises(ValueError, match="independently classified"):
        await service.receive_income(world.run_id, platform="store", body=body, headers=headers)
    communications.register_counterparty("store", "buyer1", "independent_human")
    first = await service.receive_income(world.run_id, platform="store", body=body, headers=headers)
    assert first["credited_to_budget"] and len(credits) == 1
    assert (
        await service.receive_income(world.run_id, platform="store", body=body, headers=headers)
        == first
    )
    assert len(credits) == 1 and credits[0][0][2] == "independent"
    assert footprint.query(world.run_id)[0]["readings"][0]["grounded_eligible"]
    life["ended"] = {"reason": "bankruptcy"}
    payload["external_id"] = "receipt2"
    body = json.dumps(payload).encode()
    headers["x-landing-party-signature"] = hmac.new(
        b"income-fixture-key", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    late = await service.receive_income(world.run_id, platform="store", body=body, headers=headers)
    assert not late["credited_to_budget"] and len(credits) == 1
    await service.aclose()
    await communications.aclose()
    await footprint.aclose()


@pytest.mark.parametrize("credits", ["1e300", "1e-20"])
async def test_unrepresentable_trade_is_rejected_without_changing_book(world, credits):
    service = market(world)
    evidence(world)
    with pytest.raises(ValueError):
        service.trade(world.run_id, "a_invalid", market_id="election", side="yes", credits=credits)
    assert service.balance(world.run_id)["credits"] == 1000
    assert service.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0
    await service.aclose()
