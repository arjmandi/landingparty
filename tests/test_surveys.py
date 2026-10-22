import asyncio
from datetime import timedelta

import pytest
from arena_helpers import arena_fixture


def schedule(fixture):
    return fixture.surveys.schedule(
        "l_fixture",
        fixture.participant["participant_id"],
        protocol_ref="fixture-protocol",
        questions=[{"item_id": "return", "question": "Would you independently return?"}],
        after_days=90,
    )


def end_and_advance(fixture):
    fixture.lives.life["ended"] = {"at": fixture.clock().isoformat(), "reason": "life_end"}
    fixture.clock.value += timedelta(days=90)


async def test_missing_transport_is_durable_block_not_fabricated_delivery(tmp_path):
    async with arena_fixture(tmp_path) as f:
        survey = schedule(f)
        assert survey["state"] == "awaiting_life_end"
        assert (await f.surveys.dispatch_due())["results"] == []
        end_and_advance(f)
        results = await f.surveys.dispatch_due(life_id="l_fixture", checkpoint=90)
        assert results["results"][0]["rule_id"] == "survey_transport"
        assert f.surveys.get(survey["survey_id"])["state"] == "blocked"
        assert f.surveys.report("l_fixture")["confirmed_invitations"] == 0


async def test_opt_in_and_frozen_question_binding_and_withdrawal(tmp_path):
    async with arena_fixture(tmp_path) as f:
        survey = schedule(f)
        assert schedule(f)["survey_id"] == survey["survey_id"]
        with pytest.raises(ValueError, match="cannot be replaced"):
            f.surveys.schedule(
                "l_fixture",
                f.participant["participant_id"],
                protocol_ref="fixture-protocol",
                questions=[{"item_id": "another", "question": "replacement"}],
                after_days=90,
            )
        f.arena.withdraw(f.participant["participant_id"])
        end_and_advance(f)
        assert f.surveys.get(survey["survey_id"])["state"] == "cancelled"
        assert (await f.surveys.dispatch_due())["results"] == []


async def test_confirmed_survey_response_keeps_solicited_provenance_and_biases(tmp_path):
    sent = []

    async def transport(survey, participant, consent):
        sent.append(survey["survey_id"])
        assert participant["participant_id"] == consent["participant_id"]
        return {
            "delivered": True,
            "approved_delivery": True,
            "observation_ref": "fixture-approved-delivery",
        }

    async with arena_fixture(tmp_path, transport=transport) as f:
        survey = schedule(f)
        end_and_advance(f)
        await f.surveys.dispatch_due()
        assert sent == [survey["survey_id"]]
        f.surveys.respond(
            survey["survey_id"],
            f.participant["participant_id"],
            {"return": "yes"},
            authenticated_source=True,
            observation_ref="fixture-authenticated-response",
        )
        result = f.surveys.get(survey["survey_id"])
        assert result["response_provenance"]["solicited"]
        assert not result["response_provenance"]["grounded_eligible"]
        report = f.surveys.report("l_fixture")
        assert report["response_rate"] == 1
        assert "nonresponse" in report["biases"]
        assert not report["grounded_eligible"]
        with pytest.raises(ValueError, match="accepted once"):
            f.surveys.respond(
                survey["survey_id"],
                f.participant["participant_id"],
                {"return": "changed"},
                authenticated_source=True,
                observation_ref="x",
            )


async def test_ambiguous_delivery_is_not_retried_and_concurrent_dispatch_deduplicates(tmp_path):
    attempts = []
    started, release = asyncio.Event(), asyncio.Event()

    async def ambiguous(survey, participant, consent):
        attempts.append(survey["survey_id"])
        started.set()
        await release.wait()
        raise TimeoutError("private provider response must not be logged")

    async with arena_fixture(tmp_path, transport=ambiguous) as f:
        survey = schedule(f)
        end_and_advance(f)
        first = asyncio.create_task(f.surveys.dispatch_due())
        await started.wait()
        second = await f.surveys.dispatch_due()
        assert second["results"] == []
        release.set()
        await first
        assert f.surveys.get(survey["survey_id"])["state"] == "uncertain"
        await f.surveys.dispatch_due()
        assert len(attempts) == 1


async def test_withdrawal_during_delivery_cannot_reactivate_survey(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def transport(survey, participant, consent):
        started.set()
        await release.wait()
        return {
            "delivered": True,
            "approved_delivery": True,
            "observation_ref": "fixture-inflight-receipt",
        }

    async with arena_fixture(tmp_path, transport=transport) as f:
        survey = schedule(f)
        end_and_advance(f)
        dispatch = asyncio.create_task(f.surveys.dispatch_due())
        await started.wait()
        f.arena.withdraw(f.participant["participant_id"])
        release.set()
        await dispatch
        latest = f.surveys.get(survey["survey_id"])
        assert latest["state"] == "cancelled"
        assert latest["in_flight_delivery_receipt"]["delivered"]
        assert f.surveys.report("l_fixture")["confirmed_invitations"] == 1


async def test_development_mode_never_invokes_even_a_configured_delivery_adapter(tmp_path):
    async def must_not_send(*args):
        pytest.fail("development mode must never contact survey participants")

    async with arena_fixture(tmp_path, mode="development", transport=must_not_send) as f:
        schedule(f)
        end_and_advance(f)
        result = await f.surveys.dispatch_due()
        assert result["results"][0]["rule_id"] == "survey_transport"
