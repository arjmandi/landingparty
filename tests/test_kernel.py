import asyncio
import copy
import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from worldd.beliefs import BeliefIntegrityError, BeliefValidationError
from worldd.kernel import BeliefService
from worldd.storage import Store


class WorldFixture:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path)
        self.services = {}
        self.events = []
        self.jobs = {}

    def push(self, run_id, event):
        self.events.append((run_id, event))
        return self.store.push_event(run_id, event)


@pytest.fixture
def kernel(tmp_path):
    world = WorldFixture(tmp_path)
    clock = [datetime(2026, 10, 22, 12, tzinfo=UTC)]
    run = world.store.create_run(
        {"budget": {"usd": 20}, "series_id": "s_test", "reflection_policy": "none"}
    )
    run_id = run["run_id"]
    world.store.append_record(run_id, "outcome", action_id="a_page")
    service = BeliefService(world, clock=lambda: clock[0])
    world.services["beliefs"] = service
    yield world, service, run_id, clock
    service.database.close()
    world.store.close()


def proposal(**changes):
    return {
        "kind": "fact",
        "claim": "Harbour open",
        "rank": 4,
        "triple": {"subject": "harbour", "predicate": "status", "object": "open"},
        "evidence": [{"ref": "a_page", "class": "observed"}],
        **changes,
    }


async def test_service_records_free_belief_boundary_and_projection(kernel):
    world, service, run_id, clock = kernel
    reply = await service.handle(run_id, "a_believe", "believe", {"record": proposal()})
    assert reply["kind"] == "result"
    belief_id = reply["data"]["id"]
    clock[0] += timedelta(hours=1)
    await service.observe(
        run_id,
        {
            "observation_ref": "o_change",
            "triple": {"subject": "harbour", "predicate": "status", "object": "closed"},
            "observed_at": clock[0].isoformat(),
        },
    )
    held = service.fold(run_id)[0]
    assert held["id"] == belief_id and held["rank"] == 4 and held["status"] == "held"
    assert held["kernel"]["classifications"][0]["kind"] == "revision"
    assert "flags" not in held["kernel"]
    assert service.query(run_id, query="review")[0]["id"] == belief_id
    assert world.events == []  # none arm never injects reflection
    assert world.store.total_spent(run_id) == 0
    assert any(event["kind"] == "belief_assert" for event in world.store.records(run_id))


async def test_update_closes_valid_interval_and_ignores_untrusted_source_dates(kernel):
    _, service, run_id, clock = kernel
    first = await service.handle(
        run_id, "a_1", "believe", {"record": proposal(change_class="fast")}
    )
    before = clock[0]
    clock[0] += timedelta(days=1)
    await service.observe(
        run_id,
        {
            "observation_ref": "o_closed",
            "triple": {"subject": "harbour", "predicate": "status", "object": "closed"},
            "observed_at": clock[0].isoformat(),
            "last_modified": "1900-01-01T00:00:00Z",
        },
    )
    record = service.fold(run_id)[0]
    assert record["kernel"]["classifications"][0]["kind"] == "update"
    assert record["valid_to"] == clock[0].isoformat(timespec="microseconds")
    assert service.fold(run_id, valid_at=clock[0]) == []
    assert service.fold(run_id, as_of=before)[0]["id"] == first["data"]["id"]


async def test_support_lost_is_bounded_to_three_hops_and_preserves_unrelated(kernel):
    _, service, run_id, clock = kernel
    base = (await service.handle(run_id, "a_base", "believe", {"record": proposal()}))["data"]
    parent = base
    children = []
    for index in range(4):
        parent = (
            await service.handle(
                run_id,
                f"a_{index}",
                "believe",
                {
                    "record": {
                        "kind": "strategy",
                        "claim": f"Follow plan {index}",
                        "rank": 3,
                        "links": [{"to": parent["id"], "relation": "derived_from"}],
                    }
                },
            )
        )["data"]
        children.append(parent["id"])
    unrelated = (
        await service.handle(
            run_id,
            "a_other",
            "believe",
            {
                "record": proposal(
                    claim="Unrelated rule",
                    kind="rule",
                    triple=None,
                )
            },
        )
    )["data"]
    clock[0] += timedelta(seconds=1)
    await service.observe(
        run_id, {"observation_ref": "o_bad", "belief_id": base["id"], "supports": False}
    )
    folded = {record["id"]: record for record in service.fold(run_id)}
    assert [folded[child]["kernel"]["support_lost"][0]["hop"] for child in children[:3]] == [
        1,
        2,
        3,
    ]
    assert folded[children[3]]["kernel"]["support_lost"] == []
    assert folded[unrelated["id"]]["kernel"]["support_lost"] == []
    assert folded[unrelated["id"]]["claim"] == unrelated["claim"]


async def test_fifty_record_cap_includes_attached_belief(kernel):
    _, service, run_id, _ = kernel
    base = service.beliefs(run_id).believe(proposal())
    for index in range(56):
        service.beliefs(run_id).believe(
            {
                "kind": "rule",
                "claim": f"Derived {index}",
                "rank": 3,
                "links": [{"to": base["id"], "relation": "derived_from"}],
            }
        )
    await service.observe(
        run_id, {"observation_ref": "o_mass", "belief_id": base["id"], "supports": False}
    )
    assert sum(bool(record["kernel"]["support_lost"]) for record in service.fold(run_id)) == 49


async def test_assumptions_require_pending_world_ticket_and_flip_mechanically(kernel):
    world, service, run_id, clock = kernel
    world.store.append_record(run_id, "ask_opened", ticket_id="t_access", state="open")
    rejected = await service.handle(
        run_id, "a_bad", "believe", {"record": proposal(assumptions=["t_fake"])}
    )
    assert rejected["kind"] == "blocked"
    result = await service.handle(
        run_id, "a_good", "believe", {"record": proposal(assumptions=["t_access"])}
    )
    assert result["data"]["status"] == "conditional"
    clock[0] += timedelta(seconds=1)
    await service.observe(
        run_id, {"observation_ref": "o_ticket", "ticket_id": "t_access", "outcome": "approved"}
    )
    assert service.fold(run_id)[0]["status"] == "held"
    assert service.fold(run_id)[0]["rank"] == 4


async def test_out_of_order_observations_replay_by_world_time_and_ids_are_immutable(kernel):
    _, service, run_id, clock = kernel
    service.beliefs(run_id).believe(proposal(change_class="fast"))
    clock[0] += timedelta(days=3)
    late = {
        "observation_ref": "o_second",
        "belief_id": service.fold(run_id)[0]["id"],
        "supports": False,
        "observed_at": clock[0].isoformat(),
    }
    await service.observe(run_id, late)
    earlier = {
        **late,
        "observation_ref": "o_first",
        "observed_at": (clock[0] - timedelta(days=2)).isoformat(),
    }
    await service.observe(run_id, earlier)
    record = service.fold(run_id)[0]
    assert record["valid_to"] == (clock[0] - timedelta(days=2)).isoformat(timespec="microseconds")
    assert (await service.observe(run_id, earlier))["duplicate"]
    with pytest.raises(BeliefValidationError, match="immutable"):
        await service.observe(run_id, {**earlier, "supports": True})


async def test_sealed_run_observation_has_separate_hash_journal(kernel):
    world, service, run_id, _ = kernel
    service.beliefs(run_id).believe(proposal())
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    original = (world.store.run_dir(run_id) / "record.jsonl").read_bytes()
    await service.observe(
        run_id,
        {
            "observation_ref": "o_after",
            "belief_id": service.fold(run_id)[0]["id"],
            "supports": False,
        },
    )
    assert (world.store.run_dir(run_id) / "record.jsonl").read_bytes() == original
    assert service.database.verify(run_id)["valid"]
    assert service.database.journal(run_id)[-1]["kind"] == "belief_observation"


async def test_signed_pattern_bundle_filters_low_rank_and_rejects_tampering(kernel):
    world, service, donor, _ = kernel
    high = service.beliefs(donor).believe(proposal())
    low = service.beliefs(donor).believe(proposal(claim="Tentative other view", rank=1))
    bundle = service.export_bundle(donor, tuple_={"world_id": "test"})
    assert bundle["manifest"]["belief_ids"] == [high["id"]]
    assert low["id"] not in str(bundle)
    assert "record.jsonl" not in str(bundle) and "transcript" not in bundle
    recipient = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    altered = copy.deepcopy(bundle)
    altered["deltas"][0]["belief"]["rank"] = 0
    with pytest.raises(BeliefValidationError, match="signature"):
        service.import_bundle(recipient, altered)
    imported = service.import_bundle(recipient, bundle)
    assert imported["imported"] == 1
    assert service.fold(recipient)[0]["claim"] == high["claim"]
    assert service.beliefs(recipient).why(high["id"])["evidence"] == high["evidence"]
    assert service.beliefs(recipient).verify()["valid"]


async def test_recovery_recreates_read_only_views(kernel):
    world, service, run_id, _ = kernel
    service.beliefs(run_id).believe(proposal())
    await service.recover()
    path = world.store.run_dir(run_id) / "world" / "beliefs" / "state.json"
    assert path.exists() and path.stat().st_mode & 0o222 == 0


async def test_agent_reconfirms_companion_proposal_with_a_new_version(kernel):
    _, service, run_id, _ = kernel
    proposed = await service.handle(
        run_id,
        "a_propose",
        "believe",
        {
            "record": proposal(
                author="reflector",
                status="proposed",
            )
        },
        author="reflector",
    )
    before = proposed["data"]
    assert before["author"] == "reflector"
    confirmed = await service.handle(
        run_id,
        "a_confirm",
        "revise",
        {
            "id": before["id"],
            "delta": {"status": "held"},
        },
    )
    assert confirmed["data"]["op"] == "reconfirm"
    assert confirmed["data"]["author"] == "agent"
    assert confirmed["data"]["rank"] == before["rank"]
    assert confirmed["data"]["reconfirmed_from"] == before["version_id"]


async def test_agent_cannot_forge_companion_authorship(kernel):
    _, service, run_id, _ = kernel
    reply = await service.handle(
        run_id,
        "a_forge",
        "believe",
        {
            "record": proposal(
                author="reflector",
                status="proposed",
            )
        },
    )
    assert reply["kind"] == "blocked" and service.fold(run_id) == []


async def test_source_trust_initial_condition_is_explicit_and_revisable(kernel):
    _, service, run_id, _ = kernel
    prior = service.initialize_source_trust(run_id)
    assert prior["kind"] == "rule" and "revisable" in prior["claim"]
    revised = await service.handle(
        run_id,
        "a_rule",
        "revise",
        {
            "id": prior["id"],
            "delta": {"claim": "Prefer this counterparty after repeated accurate observations."},
        },
    )
    assert revised["kind"] == "result" and revised["data"]["rank"] == prior["rank"]


async def test_pattern_bundle_preserves_late_evidence_projection(kernel):
    world, service, donor, clock = kernel
    record = service.beliefs(donor).believe(proposal(change_class="fast"))
    clock[0] += timedelta(days=1)
    await service.observe(
        donor, {"observation_ref": "o_late_pattern", "belief_id": record["id"], "supports": False}
    )
    source = service.fold(donor)[0]
    recipient = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    service.import_bundle(recipient, service.export_bundle(donor))
    imported = service.fold(recipient)[0]
    assert imported["kernel"] == source["kernel"]
    assert imported["valid_to"] == source["valid_to"]
    assert imported["rank"] == source["rank"]


async def test_procedural_skill_requires_actual_world_test_evidence_and_exports_code(
    kernel, tmp_path
):
    world, service, run_id, _ = kernel
    source = "def double(value):\n    return value * 2\n"
    tests = "from skill import double\nassert double(4) == 8\n"
    source_hash = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    tests_hash = "sha256:" + hashlib.sha256(tests.encode()).hexdigest()
    directory = tmp_path / "skill-check"
    directory.mkdir()
    (directory / "skill.py").write_text(source)
    (directory / "checks.py").write_text(tests)
    command = await asyncio.create_subprocess_exec(
        sys.executable,
        "checks.py",
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await command.communicate()
    assert command.returncode == 0
    with pytest.raises(BeliefValidationError, match="passing world test"):
        service.promote_skill(run_id, "double", source, tests, "e_nonexistent")
    world.store.append_record(
        run_id,
        "skill_test",
        event_id="e_skill_test",
        passed=True,
        tests_executed=1,
        source_hash=source_hash,
        tests_hash=tests_hash,
    )
    promoted = service.promote_skill(run_id, "double", source, tests, "e_skill_test")
    assert promoted["source_hash"] == source_hash
    bundle = service.export_bundle(run_id)
    assert bundle["skills"][0]["source"] == source
    target = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    service.import_bundle(target, bundle)
    assert service.skills(target)[0]["tests_executed"] == 1
    assert service.skills(target)[0]["source_run_id"] == run_id


async def test_continuity_preserves_full_history_low_ranks_and_conditional_state(kernel):
    world, service, source, clock = kernel
    low = service.beliefs(source).believe(proposal(rank=0))
    changed = service.beliefs(source).revise(low["id"], {"rank": 1, "claim": "Still tentative"})
    conditional = service.beliefs(source).believe(
        proposal(
            kind="rule",
            claim="Pending approval",
            triple=None,
            assumptions=["t_pending"],
            rank=2,
        )
    )
    withdrawn = service.beliefs(source).believe(proposal(rank=2, claim="Withdraw me"))
    service.beliefs(source).retract(withdrawn["id"])
    clock[0] += timedelta(seconds=1)
    await service.observe(
        source, {"observation_ref": "o_continuity", "belief_id": changed["id"], "supports": False}
    )
    before = service.fold(source)
    bundle = service.export_continuity(source)
    assert bundle["manifest"]["purpose"] == "continuity"
    assert len(bundle["deltas"]) == 5
    target = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    service.import_continuity(target, bundle)
    assert service.fold(target) == before
    assert service.beliefs(target).history(low["id"]) == service.beliefs(source).history(low["id"])
    assert (
        next(record for record in service.fold(target) if record["id"] == conditional["id"])[
            "status"
        ]
        == "conditional"
    )
    assert service.beliefs(target).verify()["valid"]
    with pytest.raises(BeliefValidationError, match="complete continuity"):
        service.import_continuity(target, service.export_bundle(source))


async def test_pattern_recipient_can_revise_belief_linked_to_nonexported_evidence(kernel):
    world, service, source, _ = kernel
    low = service.beliefs(source).believe(proposal(rank=1))
    high = service.beliefs(source).believe(
        {
            "kind": "rule",
            "claim": "Check before departure",
            "rank": 4,
            "links": [{"to": low["id"], "relation": "derived_from"}],
        }
    )
    target = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    service.import_bundle(target, service.export_bundle(source))
    reply = await service.handle(
        target,
        "a_revise_import",
        "revise",
        {
            "id": high["id"],
            "delta": {"claim": "Check twice before departure"},
        },
    )
    assert reply["kind"] == "result"
    assert service.beliefs(target).why(high["id"])["links"][0]["to"] == low["id"]


async def test_revision_override_logs_disagreement_with_mechanical_classifier(kernel):
    _, service, run_id, _ = kernel
    initial = service.beliefs(run_id).believe(proposal())
    await service.observe(
        run_id, {"observation_ref": "o_override", "belief_id": initial["id"], "supports": False}
    )
    reply = await service.handle(
        run_id,
        "a_override",
        "revise",
        {
            "id": initial["id"],
            "delta": {
                "revision": {"kind": "update", "conflict_class": "current_belief"},
            },
        },
    )
    assert reply["kind"] == "result"
    event = next(
        event
        for event in service.database.journal(run_id)
        if event["kind"] == "belief_revision_override"
    )
    assert event["kernel_kind"] == "revision" and event["agent_kind"] == "update"
    assert event["disagreement"] is True


async def test_same_life_evidence_is_resolvable_without_a_series(kernel):
    world, service, _, _ = kernel
    old = world.store.create_run({"budget": {"usd": 1}, "life_id": "l_same"})["run_id"]
    current = world.store.create_run({"budget": {"usd": 1}, "life_id": "l_same"})["run_id"]
    world.store.append_record(old, "outcome", action_id="a_old_evidence")
    reply = await service.handle(
        current,
        "a_new_belief",
        "believe",
        {
            "record": proposal(
                evidence=[
                    {
                        "ref": "a_old_evidence",
                        "class": "observed",
                    }
                ]
            )
        },
    )
    assert reply["kind"] == "result"


async def test_late_belief_event_rehydrates_original_evidence_in_active_session(kernel):
    world, service, source, clock = kernel
    original = service.beliefs(source).believe(proposal())
    current = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    service.import_continuity(current, service.export_continuity(source))
    clock[0] += timedelta(seconds=1)
    await service.observe(
        source, {"observation_ref": "o_delayed", "belief_id": original["id"], "supports": False}
    )
    await service.observe_event(
        current, {"type": "belief_review", "source_run_id": source, "observation_ref": "o_delayed"}
    )
    assert service.fold(current)[0]["n_contra"] == 1
    assert service.fold(current)[0]["kernel"]["classifications"][0]["kind"] == "revision"


async def test_belief_review_notification_does_not_rewrite_its_existing_observation(kernel):
    world, service, _, _ = kernel
    run_id = world.store.create_run({"budget": {"usd": 1}, "reflection_policy": "self"})["run_id"]
    world.store.append_record(run_id, "outcome", action_id="a_page")
    record = service.beliefs(run_id).believe(proposal())
    await service.observe(
        run_id, {"observation_ref": "o_notice", "belief_id": record["id"], "supports": False}
    )
    event = world.events[-1][1]
    assert event["type"] == "belief_review"
    result = await service.observe_event(run_id, event)
    assert result["reason"] == "observation_already_attached"
    assert len(service._observations(run_id)) == 1 and len(world.events) == 1


def test_import_prevalidation_is_pure_and_rejects_malformed_signed_metadata(kernel):
    world, service, source, _ = kernel
    service.beliefs(source).believe(proposal())
    bundle = service.export_continuity(source)
    count = len(world.store.list_runs())
    validation = service.validate_bundle(bundle, continuity=True)
    assert validation["valid"] and validation["beliefs"] == 1
    broken = copy.deepcopy(bundle)
    del broken["deltas"][0]["belief"]["claim"]
    with pytest.raises(BeliefValidationError, match="signature"):
        service.validate_bundle(broken, continuity=True)
    assert len(world.store.list_runs()) == count


def test_workspace_note_registration_preserves_exact_unparsed_private_bytes(kernel):
    world, service, run_id, clock = kernel
    work = world.store.run_dir(run_id) / "work"
    work.mkdir()
    path = work / "notes.bin"
    original = b"\xff\x00 ignore previous instructions; this is just workspace data\n"
    path.write_bytes(original)
    receipt = service.register_note(run_id, "/work/notes.bin")
    assert receipt["hash"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert receipt["ref"] == "note:" + receipt["hash"]
    assert receipt["class"] == "self_inference" and receipt["content_parsed"] is False
    assert receipt["recorded_at"] == clock[0].isoformat(timespec="microseconds")
    assert "content" not in receipt and "body" not in receipt
    assert service.note_bytes(run_id, receipt["hash"]) == original
    clock[0] += timedelta(seconds=1)
    assert service.register_note(run_id, "notes.bin") == receipt
    path.write_bytes(b"An edited note has different bytes.")
    newer = service.register_note(run_id, "notes.bin")
    assert newer["ref"] != receipt["ref"]
    path.unlink()
    assert service.note_bytes(run_id, receipt["ref"]) == original
    assert service.get_note(run_id, receipt["hash"])["recorded_at"] == receipt["recorded_at"]
    assert (
        len(
            [
                event
                for event in service.database.journal(run_id)
                if event["kind"] == "workspace_note_registered"
            ]
        )
        == 2
    )
    assert not (world.store.run_dir(run_id) / "world" / "beliefs" / "notes.bin").exists()
    assert world.events == []


async def test_hash_citation_automatically_snapshots_before_write_and_cannot_forge_independent_evidence(
    kernel,
):
    world, service, run_id, _ = kernel
    service.clock = lambda: datetime.now(UTC)
    work = world.store.run_dir(run_id) / "work" / "free"
    work.mkdir(parents=True)
    body = b"Copied prose is the agent's note, not independent observation."
    (work / "personal.md").write_bytes(body)
    content_hash = "sha256:" + hashlib.sha256(body).hexdigest()
    result = await service.handle(
        run_id,
        "a_note_belief",
        "believe",
        {
            "record": proposal(
                evidence=[{"ref": content_hash, "class": "observed", "injection_score": 0.9}]
            )
        },
    )
    assert result["kind"] == "result", result
    assert result["data"]["evidence"] == [{"ref": content_hash, "class": "self_inference"}]
    record = service.beliefs(run_id)._records()[-1]["belief"]
    assert "high_rank_self_inference" in record["flags"]
    receipt = service.get_note(run_id, content_hash)
    assert receipt["recorded_at"] <= result["data"]["recorded_at"]
    assert service.note_bytes(run_id, content_hash) == body
    assert world.store.total_spent(run_id) == 0 and world.events == []


def test_workspace_note_open_never_follows_symlinks_traversal_hardlinks_or_special_files(kernel):
    world, service, run_id, _ = kernel
    root = world.store.run_dir(run_id)
    work = root / "work"
    work.mkdir()
    outside = root / "private.txt"
    outside.write_bytes(b"World-private bytes")
    (work / "symlink").symlink_to(outside)
    (work / "directory").symlink_to(root, target_is_directory=True)
    os.link(outside, work / "hardlink")
    os.mkfifo(work / "fifo")
    for path in (
        "../private.txt",
        str(outside),
        "symlink",
        "directory/private.txt",
        "hardlink",
        "fifo",
    ):
        with pytest.raises(BeliefValidationError):
            service.register_note(run_id, path)
    assert service.database.db.execute("SELECT COUNT(*) FROM belief_notes").fetchone()[0] == 0
    assert not list((root / "beliefs" / "notes").glob(".capture-*"))


def test_note_hash_mismatch_and_concurrent_workspace_changes_never_create_receipts(
    kernel, monkeypatch
):
    world, service, run_id, _ = kernel
    work = world.store.run_dir(run_id) / "work"
    work.mkdir()
    path = work / "note"
    path.write_bytes(b"Original bytes")
    with pytest.raises(BeliefValidationError, match="expected hash"):
        service.register_note(run_id, "note", expected_hash="sha256:" + "0" * 64)
    read = os.read
    changed = False

    def mutation(descriptor, size):
        nonlocal changed
        chunk = read(descriptor, size)
        if chunk and not changed:
            changed = True
            path.write_bytes(b"Modified concurrently while the world reads")
        return chunk

    monkeypatch.setattr("worldd.kernel.os.read", mutation)
    with pytest.raises(BeliefValidationError, match="changed while"):
        service.register_note(run_id, "note")
    assert service.database.db.execute("SELECT COUNT(*) FROM belief_notes").fetchone()[0] == 0


def test_note_scope_and_restart_preserve_citations_and_detect_private_corruption(kernel):
    world, service, run_id, clock = kernel
    work = world.store.run_dir(run_id) / "work"
    work.mkdir()
    (work / "note").write_bytes(b"Immutable citation")
    receipt = service.register_note(run_id, "note")
    next_session = world.store.create_run({"budget": {"usd": 1}, "series_id": "s_test"})["run_id"]
    unrelated = world.store.create_run({"budget": {"usd": 1}, "series_id": "s_other"})["run_id"]
    assert service.get_note(next_session, receipt["hash"])["run_id"] == run_id
    with pytest.raises(KeyError):
        service.get_note(unrelated, receipt["ref"])
    assert service._resolve_evidence(unrelated, receipt["hash"]) is None
    rebuilt = BeliefService(world, clock=lambda: clock[0])
    try:
        assert rebuilt.note_bytes(run_id, receipt["hash"]) == b"Immutable citation"
        body = rebuilt._note_location(run_id, receipt["ref"]).with_suffix(".bin")
        body.chmod(0o600)
        body.write_bytes(b"Tampered world storage")
        with pytest.raises(BeliefIntegrityError, match="byte integrity"):
            rebuilt.get_note(run_id, receipt["ref"])
    finally:
        rebuilt.database.close()


async def test_note_self_inference_provenance_survives_pattern_transfer(kernel):
    world, service, source, _ = kernel
    work = world.store.run_dir(source) / "work"
    work.mkdir()
    (work / "note").write_bytes(b"Personal note")
    receipt = service.register_note(source, "note")
    record = (
        await service.handle(
            source,
            "a_note",
            "believe",
            {"record": proposal(evidence=[{"ref": receipt["hash"], "class": "self_inference"}])},
        )
    )["data"]
    bundle = service.export_bundle(source)
    assert bundle["evidence_index"][receipt["hash"]]["evidence_origin"] == "workspace_note"
    target = world.store.create_run({"budget": {"usd": 1}, "series_id": "s_other"})["run_id"]
    service.import_bundle(target, bundle)
    revised = await service.handle(
        target,
        "a_revise",
        "revise",
        {
            "id": record["id"],
            "delta": {"evidence": [{"ref": receipt["ref"], "class": "observed"}]},
        },
    )
    assert revised["kind"] == "result", revised
    assert revised["data"]["evidence"][0]["class"] == "self_inference"


def test_note_registration_after_seal_is_rejected_without_record_mutation(kernel):
    world, service, run_id, _ = kernel
    work = world.store.run_dir(run_id) / "work"
    work.mkdir()
    (work / "note").write_bytes(b"Free notes are optional")
    world.store.update_run(run_id, state="sealed")
    before = world.store.records(run_id)
    with pytest.raises(BeliefValidationError, match="running session"):
        service.register_note(run_id, "note")
    assert world.store.records(run_id) == before


@pytest.mark.parametrize(
    ("claim", "slots"),
    [
        ("The harbour is open.", {"subject": "The harbour", "predicate": "is", "object": "open"}),
        ("Water IS not red", {"subject": "Water", "predicate": "is", "object": "not red"}),
        ("capacity = -12.5", {"subject": "capacity", "predicate": "=", "object": "-12.5"}),
        (
            "The gates were closed",
            {"subject": "The gates", "predicate": "were", "object": "closed"},
        ),
    ],
)
def test_slot_suggestions_are_versioned_surface_grammar_without_semantic_mutation(
    kernel, claim, slots
):
    world, service, run_id, _ = kernel
    before = world.store.records(run_id)
    proposal_ = service.query(run_id, query="suggest_slots", claim=claim)
    assert proposal_["suggested_slots"] == slots
    assert proposal_["status"] == "proposed" and proposal_["policy_id"] == "copula-slots@1"
    assert proposal_["auto_accepted"] is False and proposal_["grammar_only"] is True
    assert service.fold(run_id) == [] and world.store.records(run_id) == before


@pytest.mark.parametrize(
    "claim",
    [
        "The harbour is open and the quay is closed.",
        "Is the harbour open?",
        "It is open.",
        "There is a harbour.",
        "The notice says 'Harbour is open'.",
        "A = B = C",
        "The harbour could open",
        "Harbour is open. Other ships wait.",
        "X is Y = Z",
    ],
)
def test_unsupported_or_ambiguous_slot_grammar_proposes_nothing(kernel, claim):
    _, service, run_id, _ = kernel
    proposal_ = service.suggest_slots(run_id, claim)
    assert proposal_["status"] == "unavailable" and proposal_["suggested_slots"] is None


async def test_missing_triple_block_only_proposes_and_agent_must_explicitly_accept(kernel):
    _, service, run_id, _ = kernel
    record = proposal(claim="The harbour is open.", triple=None)
    first = await service.handle(run_id, "a_no_slots", "believe", {"record": record})
    assert first["kind"] == "blocked" and first["slot_suggester_policy_id"] == "copula-slots@1"
    assert first["suggestion_accepted"] is False and service.fold(run_id) == []
    accepted = await service.handle(
        run_id, "a_accept", "believe", {"record": {**record, "triple": first["suggested_slots"]}}
    )
    assert accepted["kind"] == "result" and accepted["data"]["rank"] == record["rank"]
    assert accepted["data"]["claim"] == record["claim"]


async def test_disabled_slot_suggester_preserves_structural_validation_without_proposals(kernel):
    world, service, _, _ = kernel
    run_id = world.store.create_run({"budget": {"usd": 1}, "slot_suggester_policy_id": "none"})[
        "run_id"
    ]
    world.store.append_record(run_id, "outcome", action_id="a_page")
    assert service.suggest_slots(run_id, "The harbour is open.")["status"] == "disabled"
    result = await service.handle(run_id, "a_fact", "believe", {"record": proposal(triple=None)})
    assert result["kind"] == "blocked" and "suggested_slots" not in result
    assert service.fold(run_id) == []


async def test_actual_world_boundary_accepts_hash_notes_and_explicit_slot_proposals(tmp_path):
    from worldd.models import RunConfig
    from worldd.registry import RegistryService
    from worldd.runtime import World

    class NoNetwork:
        async def aclose(self):
            pass

    world = World(Store(tmp_path), NoNetwork())
    service = BeliefService(world)
    world.services["beliefs"] = service
    world.services["registry"] = RegistryService(world)
    try:
        run_id = world.create_run(RunConfig(stage=1, packs=["belief"]))["run_id"]
        body = b"A free personal note, copied nowhere by a semantic parser."
        (world.store.run_dir(run_id) / "work" / "note").write_bytes(body)
        content_hash = "sha256:" + hashlib.sha256(body).hexdigest()
        record = proposal(
            claim="The harbour is open.",
            triple=None,
            evidence=[{"ref": content_hash, "class": "observed"}],
        )
        first = await world.act(run_id, "believe", {"record": record})
        assert first["kind"] == "blocked" and first["suggestion_accepted"] is False
        accepted = await world.act(
            run_id, "believe", {"record": {**record, "triple": first["suggested_slots"]}}
        )
        assert accepted["kind"] == "result", accepted
        assert accepted["data"]["evidence"][0]["class"] == "self_inference"
        suggested = await world.act(
            run_id, "beliefs", {"query": "suggest_slots", "claim": "depth = 2"}
        )
        assert (
            suggested["kind"] == "result" and suggested["data"]["suggested_slots"]["object"] == "2"
        )
        assert service.note_bytes(run_id, content_hash) == body
        assert world.store.verify_record(run_id)["valid"]
    finally:
        await world.aclose()
