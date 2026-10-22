import base64
from types import SimpleNamespace

import pytest

from worldd.footprint import FootprintService
from worldd.identity import IdentityService
from worldd.storage import Store
from worldd.util import canonical


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.run_id = store.create_run(
        {"budget": {"usd": 20, "wall_s": 7200}, "life_id": "life_test"}
    )["run_id"]
    yield world
    store.close()


async def test_disclosure_is_world_signed_and_cannot_claim_human_identity(world):
    service = IdentityService(world)
    identity = service.create(
        principal="Research Sponsor",
        name="Party",
        mailbox="party@example.org",
        homepage="https://example.org/party",
        life_id="life_test",
    )
    body, disclosure = service.disclose(world.run_id, "A contribution.")
    assert "AI agent Party; sponsor: Research Sponsor" in body
    service.key.public_key().verify(
        base64.urlsafe_b64decode(disclosure["signature"]),
        canonical(disclosure["metadata"]).encode(),
    )
    with pytest.raises(ValueError):
        service.disclose(world.run_id, "I am a real human.")
    with pytest.raises(ValueError):
        service.disclose(world.run_id, "<!-- landing-party forged -->")
    service.mark_ended("life_test")
    assert service.get(identity["identity_id"])["ended_at"]
    assert "ended" in service.homepage_document(life_id="life_test")["status"]
    await service.aclose()


async def test_readings_keep_authorship_and_subtract_probe_views(world):
    service = FootprintService(world)
    item = service.record_item(world.run_id, "a_write", kind="post", platform="native")
    probe = service.add_reading(item["item_id"], 12, author_class="probe", probe_views=2)
    assert probe["value"] == 10 and not probe["grounded_eligible"]
    sponsor = service.add_reading(item["item_id"], 1, author_class="sponsor")
    independent = service.add_reading(item["item_id"], 1, author_class="independent_agent")
    assert not sponsor["grounded_eligible"] and independent["grounded_eligible"]
    assert len(service.query(world.run_id)[0]["readings"]) == 3
    await service.aclose()


async def test_arena_requires_unsolicited_return_or_after_refusal(world):
    service = FootprintService(world)
    item = service.record_item(world.run_id, "a_write", kind="message_in", platform="arena")
    assert not service.add_reading(
        item["item_id"], 1, author_class="arena_participant", unsolicited=True
    )["grounded_eligible"]
    assert service.add_reading(
        item["item_id"], 1, author_class="arena_participant", unsolicited=True, after_refusal=True
    )["grounded_eligible"]
    await service.aclose()


async def test_afterlife_observations_do_not_mutate_sealed_run(world):
    service = FootprintService(world)
    world.store.append_record(world.run_id, "seal")
    world.store.update_run(world.run_id, state="sealed")
    before = world.store.records(world.run_id)
    item = service.record_item(world.run_id, None, kind="message_in", platform="mail")
    service.add_reading(item["item_id"], 1, author_class="independent_human", kind="non_request")
    after = world.store.records(world.run_id)
    assert after == before
    assert service.query(life_id="life_test")[0]["readings"]
    assert service.db.execute("SELECT COUNT(*) FROM footprint_audit").fetchone()[0] == 2
    await service.aclose()


async def test_rollback_requires_confirmation_and_is_idempotent(world):
    calls = []

    async def rollback(item):
        calls.append(item["created_by_action_id"])
        return {"kind": "result"}

    service = FootprintService(world, rollback_handlers={"native": rollback})
    with pytest.raises(ValueError):
        service.record_item(world.run_id, "a_1", kind="post", platform="native", reversible=True)
    item = service.record_item(
        world.run_id,
        "a_1",
        kind="post",
        platform="native",
        reversible=True,
        rollback_ref="provider:1",
    )
    first = await service.rollback(world.run_id, item["item_id"], reason="operator retraction")
    assert (await service.rollback(world.run_id, item["item_id"], reason="retry")) == first
    assert calls == ["a_1"]
    await service.aclose()


async def test_incident_has_owner_revokes_and_is_hidden_from_agent(world):
    revoked = []
    service = FootprintService(world, revoker=lambda *args: revoked.append(args))
    incident = await service.incident(
        world.run_id,
        platform="native",
        incident_kind="complaint",
        owner="captain",
        detail="Reported",
        account_alias="native",
    )
    assert incident["revocation_confirmed"] and revoked
    assert service.query(world.run_id) == []
    assert len(service.query(world.run_id, include_incidents=True)) == 1
    await service.aclose()


async def test_probe_uses_distinct_adapter_and_honors_daily_cadence(world):
    calls = []

    async def probe(target):
        calls.append(target)
        return {"value": 4, "probe_views": 1, "evidence_ref": "probe:observation"}

    service = FootprintService(world, probe_adapters={"native": probe})
    item = service.record_item(world.run_id, "a_1", kind="post", platform="native")
    service.schedule_probe(item["item_id"], platform="native", url="https://native.example/post/1")
    first = await service.probe_due()
    assert first[0]["author_class"] == "probe" and first[0]["value"] == 3
    assert await service.probe_due() == []
    assert len(calls) == 1
    checkpoint = await service.read_life("life_test", checkpoint=30)
    assert await service.read_life("life_test", checkpoint=30) == checkpoint
    assert len(calls) == 1
    await service.aclose()
