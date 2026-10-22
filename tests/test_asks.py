import json

import pytest
from capability_helpers import CapabilityWorld

from worldd.approvals import action_hash


@pytest.fixture
def world(tmp_path):
    result = CapabilityWorld(tmp_path)
    yield result
    result.close()


def ticket(world, run_id, **changes):
    return world.services["asks"].raise_ticket(
        run_id,
        "a_pending",
        "approval",
        "Please approve",
        pending_action={"url": "https://fixture.test/post", "amount": 1},
        platform="fixture",
        scope="post",
        **changes,
    )["ticket_id"]


async def test_message_is_silence_without_ticket(world):
    run_id = world.run()
    response = await world.services["asks"].handle(
        run_id, "a_message", "ask", {"kind": "message", "text": "hello"}
    )
    assert response == {"kind": "silence", "action_id": "a_message"}
    assert world.services["asks"].list_tickets(run_id) == []


async def test_exact_approval_is_signed_and_single_use(world):
    run_id = world.run()
    asks = world.services["asks"]
    body = {"platform": "fixture", "scope": "post", "recipient": "a", "body": "one"}
    pending = await asks.require_approval(run_id, "a_one", body)
    assert pending["kind"] == "ticket"
    result = asks.answer(pending["ticket_id"], "approve", ask_class="approval", human_seconds=10)
    assert result["state"] == "approved"
    assert result["action_hash"] == action_hash(body)
    swapped = await asks.require_approval(run_id, "a_swap", {**body, "body": "two"})
    assert swapped["kind"] == "ticket"
    approved = await asks.require_approval(run_id, "a_retry", body)
    assert approved["kind"] == "result" and approved["approved"]
    assert (await asks.require_approval(run_id, "a_reuse", body))["kind"] == "ticket"
    assert world.store.inbox(run_id)["events"][-1]["state"] == "approved"


def test_silent_policy_preserves_open_queue_and_prevents_answers(world):
    run_id = world.run(reply_policy="silent")
    ticket_id = ticket(world, run_id)
    with pytest.raises(ValueError, match="silent"):
        world.services["asks"].answer(ticket_id, "approve")
    assert world.services["asks"].get(ticket_id)["state"] == "open"


def test_choice_class_rejected_only_at_answer_time(world):
    run_id = world.run()
    ticket_id = ticket(world, run_id)
    asks = world.services["asks"]
    assert asks.get(ticket_id)["ask_class"] is None
    reply = asks.answer(ticket_id, "approve", ask_class="choice")
    assert reply["state"] == "rejected" and reply["answer"]["reason"] == "policy_choice_class"


def test_standing_rejections_are_kind_platform_scope_and_audience_bound(world):
    run_id = world.run()
    asks = world.services["asks"]
    asks.answer(ticket(world, run_id), "reject", standing=True, reason="not_permitted")
    next_id = asks.raise_ticket(
        run_id,
        "a_next",
        "approval",
        pending_action={"different": True},
        platform="fixture",
        scope="post",
    )["ticket_id"]
    assert asks.get(next_id)["state"] == "rejected"
    other = asks.raise_ticket(
        run_id,
        "a_other",
        "approval",
        pending_action={"another": True},
        platform="fixture",
        scope="message",
    )["ticket_id"]
    assert asks.get(other)["state"] == "open"
    assert asks.get(ticket(world, world.run()))["state"] == "open"


def test_ticket_deduplication_does_not_replace_pending_body(world):
    run_id = world.run()
    first = ticket(world, run_id)
    assert ticket(world, run_id) == first
    assert len(world.services["asks"].list_tickets(run_id)) == 1


def test_expiry_and_ignored_are_distinct_and_world_timed(world):
    run_id = world.run()
    asks = world.services["asks"]
    first = ticket(world, run_id)
    ignored = asks.raise_ticket(run_id, "a_ignored", "approval", pending_action={"another": True})[
        "ticket_id"
    ]
    asks.mark_ignored(ignored)
    world.clock.advance(7 * 86400 + 1)
    assert asks.expire() == 1
    assert asks.get(first)["state"] == "expired"
    assert asks.get(ignored)["state"] == "ignored"


def test_deposit_emits_accepted_then_complete_without_secret(world):
    run_id = world.run()
    asks = world.services["asks"]
    pending = asks.raise_ticket(
        run_id,
        "a_credential",
        "grant_credential",
        pending_action={"alias": "fixture", "password": "must-not-persist"},
    )
    asks.answer(pending["ticket_id"], "deposit")
    asks.complete_deposit(pending["ticket_id"])
    events = world.store.inbox(run_id)["events"]
    assert [event["state"] for event in events] == ["accepted", "deposited"]
    assert "must-not-persist" not in json.dumps(world.store.records(run_id))
    assert "must-not-persist" not in json.dumps(asks.get(pending["ticket_id"]))


def test_active_human_seconds_capped_and_parallel_is_not_agent_spend(world):
    run_id = world.run()
    asks, ticket_id = world.services["asks"], ticket(world, run_id)
    asks.touch(ticket_id)
    world.clock.advance(900)
    reply = asks.answer(ticket_id, "reject")
    assert reply["human_seconds"] == 600
    assert asks.human_spend(run_id)["seconds"] == 600
    assert world.store.total_spent(run_id) == 0


def test_in_budget_human_time_is_actually_debited(world):
    run_id = world.run(human_time_priced="in_budget", human_rate_usd_per_minute=0.6)
    asks = world.services["asks"]
    asks.answer(ticket(world, run_id), "approve", human_seconds=30)
    assert world.store.spend(run_id)["human"] == 0.3
    assert world.store.reconcile(run_id)["valid"]


def test_approval_cannot_be_tampered_in_database(world):
    run_id = world.run()
    asks = world.services["asks"]
    ticket_id = ticket(world, run_id)
    asks.answer(ticket_id, "approve")
    asks.database.db.execute("UPDATE approval_grants SET signature='tampered'")
    with pytest.raises(ValueError, match="signature"):
        asks.approvals.consume(run_id, {"url": "https://fixture.test/post", "amount": 1})


def test_run_stop_cancels_open_tickets_and_revokes_unused_grants(world):
    run_id = world.run()
    asks = world.services["asks"]
    approved = ticket(world, run_id)
    asks.answer(approved, "approve")
    pending = asks.raise_ticket(run_id, "a_pending2", "approval", pending_action={"other": True})
    assert asks.cancel_run(run_id) == 1
    assert asks.get(pending["ticket_id"])["state"] == "cancelled"
    assert asks.approvals.consume(run_id, {"url": "https://fixture.test/post", "amount": 1}) is None


async def test_agent_cannot_invent_pending_action_for_approval(world):
    run_id = world.run()
    result = await world.services["asks"].handle(
        run_id, "a_ask", "ask", {"kind": "approval", "text": "please", "action_ref": "a_not_here"}
    )
    assert result["kind"] == "blocked" and result["rule_id"] == "ask_action_ref"


def test_takeover_approval_completes_only_after_human_done(world):
    run_id = world.run()
    asks = world.services["asks"]
    pending = asks.raise_ticket(
        run_id, "a_take", "takeover", pending_action={"session_id": "session_test"}
    )
    assert asks.answer(pending["ticket_id"], "approve")["state"] == "accepted"
    assert asks.complete_takeover(pending["ticket_id"], human_seconds=42)["state"] == "approved"
    assert asks.human_spend(run_id)["seconds"] == 42


def test_active_in_budget_human_work_reserves_before_spend_and_settles_on_stop(world):
    run_id = world.run(human_time_priced="in_budget", human_rate_per_minute=0.6)
    asks, ticket_id = world.services["asks"], ticket(world, run_id)
    assert asks.touch(ticket_id)["active"]
    assert world.store.reserved(run_id) == 6
    assert not world.store.debit(run_id, "model", 15)
    world.clock.advance(60)
    world.store.update_run(run_id, state="stopping")
    asks.cancel_run(run_id)
    assert world.store.reserved(run_id) == 0
    assert world.store.spend(run_id)["human"] == 0.6
    assert world.store.reconcile(run_id)["valid"]


def test_insufficient_budget_cannot_start_priced_console_work(world):
    run_id = world.run(
        budget={"usd": 1, "wall_s": 7200}, human_time_priced="in_budget", human_rate_per_minute=0.6
    )
    asks, ticket_id = world.services["asks"], ticket(world, run_id)
    assert asks.touch(ticket_id)["rule_id"] == "human_budget"
    assert asks._row(ticket_id)["active_since"] is None


def test_signed_approval_cannot_be_rebound_by_changing_database_index(world):
    run_id = world.run()
    asks, ticket_id = world.services["asks"], ticket(world, run_id)
    asks.answer(ticket_id, "approve")
    swapped = {"url": "https://different.test/", "amount": 999}
    asks.database.db.execute("UPDATE approval_grants SET action_hash=?", (action_hash(swapped),))
    with pytest.raises(ValueError, match="binding"):
        asks.approvals.consume(run_id, swapped)


@pytest.fixture
async def life_world(world):
    from worldd.identity import IdentityService
    from worldd.lives import LifeService

    lives = LifeService(world, clock=world.clock)
    identities = IdentityService(world)
    world.services.update(lives=lives, identity=identities)
    life_id = lives.create({"stage": 2, "budget": {"usd_life": 20}})["life_id"]
    identities.create(
        life_id=life_id,
        principal="researcher_fixture",
        name="Fixture agent",
        mailbox="fixture@example.test",
        homepage="https://fixture.test/agent",
    )
    yield world, lives, identities, life_id
    await lives.aclose()
    await identities.aclose()


def life_session(world, lives, life_id, **changes):
    config = lives.prepare_session(
        life_id,
        {"stage": 2, "budget": {"usd": 20, "wall_s": 7200}, **changes},
    )
    run_id = world.store.create_run(config)["run_id"]
    lives.attach_session(life_id, run_id)
    return run_id


def seal_session(world, lives, run_id):
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    lives.session_ended(run_id)


async def test_sleep_preserves_blocker_and_settles_human_time_but_cancels_takeover(life_world):
    world, lives, _, life_id = life_world
    run_id = life_session(
        world, lives, life_id, human_time_priced="in_budget", human_rate_per_minute=0.6
    )
    asks, pending = world.services["asks"], ticket(world, run_id)
    takeover = asks.raise_ticket(run_id, "a_take", "takeover", pending_action={"session_id": "s"})
    asks.touch(pending)
    world.clock.advance(60)
    world.store.update_run(run_id, state="stopping")
    assert asks.cancel_run(run_id, preserve_pending=True) == 1
    assert asks.get(pending)["state"] == "open"
    assert asks.get(takeover["ticket_id"])["state"] == "cancelled"
    assert world.store.spend(run_id)["human"] == 0.6
    assert world.store.reserved(run_id) == 0


async def test_late_approval_transfers_once_only_to_registered_same_life_successor(life_world):
    world, lives, _, life_id = life_world
    source = life_session(world, lives, life_id)
    asks, pending = world.services["asks"], ticket(world, source)
    asks.cancel_run(source, preserve_pending=True)
    seal_session(world, lives, source)
    sealed = world.store.records(source)
    body = {"url": "https://fixture.test/post", "amount": 1}
    asks.answer(pending, "approve", ask_class="approval")
    successor = life_session(world, lives, life_id)
    other = world.run()
    assert asks.approvals.consume(other, body) is None
    approved = await asks.require_approval(successor, "a_resume", body)
    assert approved["approved"]
    proof = next(r for r in world.store.records(successor) if r["kind"] == "approval_transferred")
    assert proof["source_run_id"] == source and proof["action_hash"] == action_hash(body)
    assert world.store.records(source) == sealed
    assert world.store.verify_record(source)["valid"]
    assert asks.approvals.consume(successor, body) is None


async def test_transfer_rejects_changed_identity_and_changed_action(life_world):
    world, lives, _, life_id = life_world
    source = life_session(world, lives, life_id)
    asks, pending = world.services["asks"], ticket(world, source)
    asks.answer(pending, "approve")
    seal_session(world, lives, source)
    successor = life_session(world, lives, life_id, identity_id="some-other-identity")
    body = {"url": "https://fixture.test/post", "amount": 1}
    assert asks.approvals.consume(successor, body) is None
    assert asks.approvals.consume(successor, {**body, "amount": 2}) is None


async def test_late_priced_console_reserves_lifetime_budget_without_mutating_source(life_world):
    world, lives, _, life_id = life_world
    source = life_session(
        world, lives, life_id, human_time_priced="in_budget", human_rate_per_minute=0.6
    )
    asks, pending = world.services["asks"], ticket(world, source)
    asks.cancel_run(source, preserve_pending=True)
    seal_session(world, lives, source)
    sealed = world.store.records(source)
    assert asks.touch(pending)["active"]
    assert lives.status(life_id)["reserved_usd"] == 6
    world.clock.advance(60)
    assert asks.answer(pending, "reject")["human_seconds"] == 60
    assert lives.status(life_id)["reserved_usd"] == 0
    assert lives.status(life_id)["spent_usd"] == 0.6
    assert world.store.records(source) == sealed
    assert len(lives.get(life_id)["sessions"]) == 1


async def test_life_terminal_close_cancels_preserved_blockers(life_world):
    world, lives, _, life_id = life_world
    source = life_session(world, lives, life_id)
    asks, pending = world.services["asks"], ticket(world, source)
    seal_session(world, lives, source)
    assert asks.cancel_life(life_id) == 1
    assert asks.get(pending)["state"] == "cancelled"
    with pytest.raises(ValueError, match="Only open"):
        asks.answer(pending, "approve")


def test_expired_accepted_deposit_cannot_resume_human_work_or_complete(world):
    run_id = world.run()
    asks = world.services["asks"]
    pending = asks.raise_ticket(
        run_id, "a_deposit", "grant_credential", pending_action={"alias": "fixture"}
    )
    asks.answer(pending["ticket_id"], "deposit")
    world.clock.advance(7 * 86400 + 1)
    with pytest.raises(ValueError, match="no longer open"):
        asks.touch(pending["ticket_id"])
    with pytest.raises(ValueError, match="accepted"):
        asks.complete_deposit(pending["ticket_id"])
    assert asks.get(pending["ticket_id"])["state"] == "expired"
