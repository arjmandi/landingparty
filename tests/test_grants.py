import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from worldd.grants import GrantService
from worldd.storage import Store, content_hash


@pytest.fixture
async def setup(tmp_path):
    store = Store(tmp_path / "data")
    world = SimpleNamespace(store=store, services={})
    world.push = store.push_event
    clock = SimpleNamespace(now=datetime.now(UTC))
    calls = []

    def fixture_proxy(request):
        calls.append({"method": request.method, "url": str(request.url)})
        return httpx.Response(200, json={"fixture": "archived response"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fixture_proxy))

    async def recorded_proxy(grant_id, action, *, run_id, archive_in):
        response = await client.request(action.get("method", "GET"), action["url"])
        return {
            "mode": "rehearsal",
            "passed": response.status_code == 200,
            "proxy_id": "injected-software-fixture",
            "archive_id": archive_in,
            "record_ref": "fixture_proxy:" + str(len(calls)),
            "request_hash": content_hash(action),
            "response_hash": content_hash(response.json()),
        }

    grants = GrantService(world, clock=lambda: clock.now, rehearsal_executor=recorded_proxy)
    world.services["grants"] = grants
    run_id = store.create_run({"budget": {"usd": 1}, "stage": 2, "research_mode": "development"})[
        "run_id"
    ]
    yield world, grants, clock, run_id, calls
    await grants.aclose()
    await client.aclose()
    store.close()


def propose(grants, run_id, **values):
    return grants.grant(
        run_id,
        packs=["mail"],
        action_classes=["message"],
        platform="fixture",
        origins=["https://example.com"],
        path_prefixes=["/messages"],
        purpose="Approved fixture correspondence",
        **values,
    )


async def activate(grants, grant):
    for _ in range(10):
        reply = await grants.rehearse(
            grant["grant_id"],
            {
                "action_class": "message",
                "platform": "fixture",
                "url": "https://example.com/messages/new",
            },
        )
        assert reply["kind"] == "result", reply
    return grants.activate(
        grant["grant_id"],
        {
            "decision": "pass",
            "reviewer": "captain",
            "rehearsal_hash": grants.rehearsal_hash(grant["grant_id"]),
        },
    )


async def test_clearance_is_pending_until_ten_actual_proxy_calls_and_bound_verdict(setup):
    world, grants, _, run_id, calls = setup
    grant = propose(grants, run_id)
    assert grants.packs(run_id) == []
    assert (
        grants.require(
            run_id, "message", platform="fixture", url="https://example.com/messages/new"
        )["kind"]
        == "blocked"
    )
    with pytest.raises(ValueError, match="ten completed"):
        grants.activate(
            grant["grant_id"],
            {"decision": "pass", "reviewer": "captain", "rehearsal_hash": "invented"},
        )
    live = await activate(grants, grant)
    assert live["state"] == "live"
    assert len(calls) == 10
    assert grants.packs(run_id) == ["mail"]
    assert grants.tools(run_id) == ["send"]
    event = world.store.inbox(run_id)["events"][-1]
    assert event["type"] == "grant" and event["tools"] == ["send"]
    assert grants.verify(run_id)["valid"]


async def test_fabricated_receipt_and_caller_boolean_cannot_replace_proxy_execution(setup):
    _, grants, _, run_id, calls = setup
    grant = propose(grants, run_id)
    with pytest.raises(ValueError, match="actual executed"):
        grants.record_rehearsal(grant["grant_id"], "invented", verdict="pass")
    assert calls == []
    assert grants.get(grant["grant_id"])["rehearsal"] == []


async def test_rehearsal_quota_is_ten_and_receipt_cannot_be_counted_twice(setup):
    _, grants, _, run_id, calls = setup
    grant = propose(grants, run_id)
    for _ in range(10):
        reply = await grants.rehearse(
            grant["grant_id"],
            {
                "action_class": "message",
                "platform": "fixture",
                "url": "https://example.com/messages/new",
            },
        )
    assert len(grants.record_rehearsal(grant["grant_id"], reply["receipt_id"])["rehearsal"]) == 10
    blocked = await grants.rehearse(
        grant["grant_id"],
        {
            "action_class": "message",
            "platform": "fixture",
            "url": "https://example.com/messages/new",
        },
    )
    assert blocked["rule_id"] == "rehearsal_quota"
    assert len(calls) == 10


async def test_reviewer_cannot_change_failed_proxy_outcome(setup):
    _, grants, _, run_id, _ = setup

    async def denied(grant_id, action, **kwargs):
        return {
            "mode": "rehearsal",
            "passed": False,
            "record_ref": "failed-archive-fetch",
            "request_hash": content_hash(action),
            "response_hash": content_hash({"blocked": True}),
        }

    grants.rehearsal_executor = denied
    grant = propose(grants, run_id)
    for _ in range(10):
        await grants.rehearse(
            grant["grant_id"],
            {
                "action_class": "message",
                "platform": "fixture",
                "url": "https://example.com/messages/new",
            },
        )
    with pytest.raises(ValueError, match="passing proxy"):
        grants.activate(
            grant["grant_id"],
            {
                "decision": "pass",
                "reviewer": "captain",
                "rehearsal_hash": grants.rehearsal_hash(grant["grant_id"]),
            },
        )


async def test_origin_path_platform_and_single_use_scopes_fail_closed(setup):
    _, grants, _, run_id, _ = setup
    grant = await activate(grants, propose(grants, run_id))
    for url in (
        "https://elsewhere.com/messages/new",
        "https://example.com/messages-elsewhere",
        "https://example.com/messages/%2e%2e/settings",
        "http://example.com/messages/new",
    ):
        assert grants.require(run_id, "message", platform="fixture", url=url)["kind"] == "blocked"
    assert (
        grants.require(run_id, "message", platform="other", url="https://example.com/messages/new")[
            "kind"
        ]
        == "blocked"
    )
    allowed = grants.require(
        run_id,
        "message",
        platform="fixture",
        url="https://example.com/messages/new",
        consume=True,
        action_id="a_once",
        tool="send",
    )
    assert allowed["authorized"] and allowed["requires_exact_approval"]
    assert grants.current(run_id) == []
    assert grants.packs(run_id) == []
    assert grants.get(grant["grant_id"])["uses"] == 1
    assert (
        grants.require(
            run_id,
            "message",
            platform="fixture",
            url="https://example.com/messages/new",
            consume=True,
            action_id="a_second",
        )["kind"]
        == "blocked"
    )


async def test_standing_grant_is_scoped_revocable_and_does_not_replay_action(setup):
    _, grants, _, run_id, _ = setup
    grant = await activate(grants, propose(grants, run_id, standing=True))
    for number in range(3):
        assert grants.require(
            run_id,
            "message",
            platform="fixture",
            url="https://example.com/messages/new",
            consume=True,
            action_id=f"a_{number}",
        )["authorized"]
    replay = grants.require(
        run_id,
        "message",
        platform="fixture",
        url="https://example.com/messages/new",
        consume=True,
        action_id="a_0",
    )
    assert replay["rule_id"] == "grant_replay"
    grants.revoke(grant["grant_id"], reason="captain withdrawal")
    assert grants.packs(run_id) == []
    assert grants.get(grant["grant_id"])["state"] == "revoked"


async def test_expiry_and_restart_retain_signed_ledger(setup):
    world, grants, clock, run_id, _ = setup
    grant = await activate(grants, propose(grants, run_id, standing=True))
    restored = GrantService(world, clock=lambda: clock.now)
    assert restored.verify(run_id)["valid"]
    assert restored.packs(run_id) == ["mail"]
    clock.now += timedelta(hours=3)
    assert restored.packs(run_id) == []
    await restored.recover()
    assert restored.get(grant["grant_id"])["state"] == "expired"
    assert restored.verify(run_id)["valid"]


async def test_incident_revokes_matching_and_broad_standing_grants(setup):
    _, grants, _, run_id, _ = setup
    grant = await activate(grants, propose(grants, run_id, standing=True))
    result = grants.revoke_incident(run_id, platform="fixture", incident_ref="incident:complaint")
    assert result["revoked"] == [grant["grant_id"]]
    assert grants.get(grant["grant_id"])["revocation_reason"] == "incident:incident:complaint"
    assert (
        grants.require(
            run_id, "message", platform="fixture", url="https://example.com/messages/new"
        )["kind"]
        == "blocked"
    )


async def test_signed_deltas_and_materialized_capabilities_detect_tampering(setup):
    world, grants, _, run_id, _ = setup
    grant = await activate(grants, propose(grants, run_id, standing=True))
    altered = grants.get(grant["grant_id"])
    altered["origins"] = ["https://evil.example"]
    world.store.db.execute(
        "UPDATE capability_clearances SET payload=? WHERE grant_id=?",
        (json.dumps(altered), grant["grant_id"]),
    )
    assert not grants.verify(run_id)["valid"]
    assert grants.current(run_id) == []
    assert (
        grants.require(
            run_id, "message", platform="fixture", url="https://evil.example/messages/new"
        )["kind"]
        == "blocked"
    )


async def test_delta_signature_rejects_a_recomputed_unkeyed_hash(setup):
    world, grants, _, run_id, _ = setup
    propose(grants, run_id)
    event = grants.ledger(run_id)[0]
    event["data"]["grant"]["standing"] = True
    event["hash"] = content_hash({key: value for key, value in event.items() if key != "hash"})
    world.store.db.execute(
        "UPDATE capability_deltas SET payload=? WHERE run_id=? AND seq=1",
        (json.dumps(event), run_id),
    )
    assert not grants.verify(run_id)["valid"]


async def test_pack_stage_and_exact_tool_manifest_cannot_be_broadened(setup):
    world, grants, _, run_id, _ = setup
    stage_zero = world.store.create_run({"budget": {"usd": 1}, "stage": 0})["run_id"]
    with pytest.raises(ValueError, match="stage gate"):
        propose(grants, stage_zero)
    with pytest.raises(ValueError, match="invent"):
        grants.grant(run_id, tools=["invented"], purpose="bad")
    grant = grants.grant(
        run_id, tools=["send"], action_classes=["message"], purpose="specific tool", standing=True
    )
    for _ in range(10):
        await grants.rehearse(
            grant["grant_id"],
            {"action_class": "message", "url": "https://example.com/messages/new"},
        )
    grants.activate(
        grant["grant_id"],
        {
            "decision": "pass",
            "reviewer": "captain",
            "rehearsal_hash": grants.rehearsal_hash(grant["grant_id"]),
        },
    )
    assert grants.packs(run_id) == []
    assert grants.tools(run_id) == ["send"]


async def test_missing_proxy_is_blocked_without_claiming_rehearsal(setup):
    _, grants, _, run_id, calls = setup
    grant = propose(grants, run_id)
    grants.rehearsal_executor = None
    result = await grants.rehearse(
        grant["grant_id"],
        {
            "action_class": "message",
            "platform": "fixture",
            "url": "https://example.com/messages/new",
        },
    )
    assert result["rule_id"] == "rehearsal_proxy"
    assert calls == [] and grants.get(grant["grant_id"])["rehearsal"] == []


async def test_cancelled_proxy_calls_consume_quota_without_fabricating_passes(setup):
    _, grants, _, run_id, _ = setup
    admitted = asyncio.Event()

    async def pending_proxy(*args, **kwargs):
        admitted.set()
        await asyncio.Event().wait()

    grants.rehearsal_executor = pending_proxy
    grant = propose(grants, run_id)
    for _ in range(10):
        admitted.clear()
        task = asyncio.create_task(
            grants.rehearse(
                grant["grant_id"],
                {
                    "action_class": "message",
                    "platform": "fixture",
                    "url": "https://example.com/messages/new",
                },
            )
        )
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    current = grants.get(grant["grant_id"])
    assert current["rehearsal_attempts"] == 10
    assert len(current["rehearsal"]) == 10
    assert not any(outcome["passed"] for outcome in current["rehearsal"])
    refused = await grants.rehearse(
        grant["grant_id"],
        {
            "action_class": "message",
            "platform": "fixture",
            "url": "https://example.com/messages/new",
        },
    )
    assert refused["rule_id"] == "rehearsal_quota"
    assert grants.verify(run_id)["valid"]


async def test_incident_revokes_same_life_old_and_current_platform_clearances(setup):
    world, grants, _, _, _ = setup

    def run(life):
        return world.store.create_run({"life_id": life, "budget": {"usd": 1}, "stage": 2})["run_id"]

    earlier, current, unrelated = run("l_one"), run("l_one"), run("l_two")
    old_grant = propose(grants, earlier, standing=True)
    new_grant = propose(grants, current, standing=True)
    other_grant = propose(grants, unrelated, standing=True)
    result = grants.revoke_incident(
        current, platform="fixture", incident_ref="incident:actual-removal"
    )
    assert set(result["revoked"]) == {old_grant["grant_id"], new_grant["grant_id"]}
    assert grants.get(other_grant["grant_id"])["state"] == "rehearsal"
    assert grants.verify(earlier)["valid"] and grants.verify(current)["valid"]
    assert (
        grants.revoke_incident(current, platform="fixture", incident_ref="incident:retry")[
            "revoked"
        ]
        == []
    )
