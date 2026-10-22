import asyncio
from datetime import timedelta

import pytest
from arena_helpers import arena_fixture


async def test_arena_binds_protocol_consent_and_world_observed_timeline(tmp_path):
    async with arena_fixture(tmp_path) as fixture:
        arena = fixture.arena
        assert (
            arena.consent(fixture.participant["participant_id"])["protocol_ref"]
            == "fixture-protocol"
        )
        events = arena.timeline(fixture.data["arena_id"])
        assert [event["kind"] for event in events] == [
            "arena_created",
            "participant_registered",
            "consent_recorded",
        ]
        assert events[-1]["prev_hash"] == events[-2]["hash"]
        assert events[-1]["consent"]["document_hash"].startswith("sha256:")
        with pytest.raises(ValueError, match="hash mismatch"):
            arena.record_consent(
                fixture.participant["participant_id"],
                document_ref="consent-fixture.txt",
                document_hash="sha256:" + "0" * 64,
                confirmed_by="fixture",
                signed_at=fixture.clock().isoformat(),
                scopes=["arena"],
            )


async def test_arena_exchange_free_action_and_after_refusal_attribution(tmp_path):
    async with arena_fixture(tmp_path) as f:
        args = (f.data["arena_id"], f.participant["participant_id"])
        first = f.arena.record_exchange(
            *args,
            run_id=f.run_id,
            direction="inbound",
            content="Hello",
            unsolicited=True,
            authenticated_source=True,
            observation_ref="fixture-inbound-1",
        )
        assert not first["event"]["grounded_eligible"]
        f.arena.record_exchange(
            *args,
            run_id=f.run_id,
            direction="outbound",
            content="No discount",
            refusal=True,
            authenticated_source=True,
            observation_ref="fixture-refusal",
        )
        returned = f.arena.record_exchange(
            *args,
            run_id=f.run_id,
            direction="inbound",
            content="I still want to work together",
            unsolicited=True,
            authenticated_source=True,
            observation_ref="fixture-return",
        )
        assert returned["event"]["after_refusal"]
        assert returned["event"]["grounded_eligible"]
        assert f.footprint.readings[-1][1]["author_class"] == "arena_participant"
        assert f.footprint.readings[-1][1]["after_refusal"]
        f.arena.withdraw(f.participant["participant_id"])
        blocked = f.arena.record_exchange(
            *args,
            run_id=f.run_id,
            direction="inbound",
            content="after withdrawal",
            authenticated_source=True,
            observation_ref="fixture-late",
        )
        assert blocked["rule_id"] == "arena_consent"


async def test_arena_rejects_unauthenticated_or_cross_life_observations(tmp_path):
    async with arena_fixture(tmp_path) as f:
        with pytest.raises(ValueError, match="authenticated"):
            f.arena.record_exchange(
                f.data["arena_id"],
                f.participant["participant_id"],
                run_id=f.run_id,
                direction="inbound",
                content="forged",
            )
        other = f.store.create_run({"budget": {"usd": 1}, "life_id": "l_other"})
        with pytest.raises(ValueError, match="different life"):
            f.arena.record_exchange(
                f.data["arena_id"],
                f.participant["participant_id"],
                run_id=other["run_id"],
                direction="inbound",
                content="forged",
                authenticated_source=True,
                observation_ref="fixture-crosslife",
            )


async def test_missing_settlement_never_produces_fictional_money(tmp_path):
    async with arena_fixture(tmp_path) as f:
        ticket = f.arena.request_stake(
            f.data["arena_id"],
            f.participant["participant_id"],
            question="choose",
            choices=["a", "b"],
            ceiling_usd=0.5,
            observation_ref="fixture-choice-bank",
        )
        assert ticket["kind"] == "ticket"
        result = await f.arena.settle_stake(ticket["ticket_id"], "a")
        assert result["rule_id"] == "stake_settlement"
        assert not [
            event
            for event in f.arena.timeline(f.data["arena_id"])
            if event["kind"] == "stake_settled"
        ]


async def test_real_stake_callback_is_single_use_and_development_cannot_call_it(tmp_path):
    calls = []

    async def settle(stake, choice):
        calls.append((stake["stake_id"], choice))
        await asyncio.sleep(0)
        return {
            "settled": True,
            "rail": "real_usd",
            "amount_usd": 0.5,
            "observation_ref": "fixture-receipt-not-money",
        }

    async with arena_fixture(tmp_path / "research-component", stake_settler=settle) as f:
        ticket = f.arena.request_stake(
            f.data["arena_id"],
            f.participant["participant_id"],
            question="choose",
            choices=["a", "b"],
            ceiling_usd=0.5,
            observation_ref="fixture-choice-bank",
        )
        results = await asyncio.gather(
            f.arena.settle_stake(ticket["ticket_id"], "a"),
            f.arena.settle_stake(ticket["ticket_id"], "a"),
            return_exceptions=True,
        )
        assert len(calls) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
    async with arena_fixture(
        tmp_path / "development", mode="development", stake_settler=settle
    ) as f:
        ticket = f.arena.request_stake(
            f.data["arena_id"],
            f.participant["participant_id"],
            question="choose",
            choices=["a", "b"],
            ceiling_usd=0.5,
            observation_ref="fixture-choice-bank",
        )
        assert (await f.arena.settle_stake(ticket["ticket_id"], "a"))[
            "rule_id"
        ] == "development_no_writes"
        assert len(calls) == 1


async def test_expired_consent_cannot_authorize_an_action(tmp_path):
    async with arena_fixture(tmp_path) as f:
        f.clock.value += timedelta(days=181)
        assert f.arena.consent(f.participant["participant_id"]) is None
