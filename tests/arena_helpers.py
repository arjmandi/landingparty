"""Non-scientific component fixtures; these never populate the real gate registry."""

import hashlib
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from worldd.arena import ArenaService
from worldd.storage import Store
from worldd.surveys import SurveyService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value


class FixtureRegistry:
    """Exercise component branches, not RegistryService's research authority."""

    def require_stage(self, stage, research_mode="research"):
        return {"research_authorized": research_mode == "research", "fixture_only": True}

    def gates(self):
        return [{"name": "IRB", "eligible": True, "report": {"protocol_ref": "fixture-protocol"}}]


class FixtureLives:
    def __init__(self, clock, mode):
        self.life = {
            "life_id": "l_fixture",
            "stage": 3,
            "research_mode": mode,
            "binding_id": "company-fixture",
            "created_at": clock().isoformat(),
            "ended": None,
        }

    def get(self, life_id):
        assert life_id == self.life["life_id"]
        return deepcopy(self.life)


class Footprint:
    def __init__(self):
        self.items, self.readings = [], []

    def record_item(self, *args, **kwargs):
        self.items.append((args, kwargs))
        return {"item_id": "fp_fixture"}

    def add_reading(self, *args, **kwargs):
        self.readings.append((args, kwargs))


@asynccontextmanager
async def arena_fixture(tmp_path, *, mode="research", transport=None, stake_settler=None):
    clock = Clock()
    store = Store(tmp_path)
    lives, footprint = FixtureLives(clock, mode), Footprint()
    world = SimpleNamespace(
        store=store,
        services={"lives": lives, "registry": FixtureRegistry(), "footprint": footprint},
    )
    arena = ArenaService(world, clock=clock, stake_settler=stake_settler)
    world.services["arena"] = arena
    surveys = SurveyService(world, transport, clock=clock)
    world.services["surveys"] = surveys
    data = arena.create(
        {
            "life_id": "l_fixture",
            "protocol_ref": "fixture-protocol",
            "consent_version": "fixture-consent-v1",
            "binding_id": "company-fixture",
            "max_stake_usd": 1,
        }
    )
    participant = arena.register_participant(
        data["arena_id"],
        role="customer",
        contact_ref="opaque-fixture-mailbox",
        participant_ref="fixture-person",
    )
    consent_path = store.data_dir / "consent-fixture.txt"
    body = b"Software test fixture, not legal consent and not a scientific gate."
    consent_path.write_bytes(body)
    consent = arena.record_consent(
        participant["participant_id"],
        document_ref=consent_path.name,
        document_hash="sha256:" + hashlib.sha256(body).hexdigest(),
        confirmed_by="fixture-only-control",
        signed_at=(clock() - timedelta(minutes=1)).isoformat(),
        scopes=["arena", "lottery", "survey"],
        expires_at=(clock() + timedelta(days=180)).isoformat(),
        survey_opt_in=True,
    )
    run = store.create_run({"budget": {"usd": 1, "wall_s": 3600}, "life_id": "l_fixture"})
    try:
        yield SimpleNamespace(
            world=world,
            store=store,
            lives=lives,
            arena=arena,
            surveys=surveys,
            clock=clock,
            data=data,
            participant=participant,
            consent=consent,
            footprint=footprint,
            run_id=run["run_id"],
        )
    finally:
        await surveys.aclose()
        await arena.aclose()
        store.close()
