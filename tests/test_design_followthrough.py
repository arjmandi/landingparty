"""Design conditions and persisted event clocks at the root integration boundary."""

import asyncio
import io
import tarfile

import httpx
import pytest

from worldd.app import create_app
from worldd.models import RunConfig, manifest
from worldd.util import digest
from worldd.versioning import implementation_manifest


@pytest.fixture
async def world(tmp_path):
    result = create_app(tmp_path).state.world
    yield result
    await result.aclose()


async def test_runtime_conditions_and_seed_scope_are_frozen(world):
    first = world.create_run(RunConfig(max_in_flight=2, seed=0))
    second = world.create_run(RunConfig(max_in_flight=2, seed=10))
    third = world.create_run(RunConfig(max_in_flight=3, seed=0))
    configs = [world.run_config(row["run_id"]) for row in (first, second, third)]
    assert configs[0]["tuple"]["runtime_protocol_id"] == configs[1]["tuple"]["runtime_protocol_id"]
    assert configs[0]["tuple"]["runtime_protocol_id"] != configs[2]["tuple"]["runtime_protocol_id"]
    assert configs[0]["seed_scope"] in first["preamble"]


async def test_stage_zero_message_preserves_progress_provenance_and_silence(world):
    run_id = world.create_run(RunConfig())["run_id"]
    reply = await world.act(
        run_id,
        "ask",
        {
            "kind": "message",
            "text": "Next choice?",
            "subgoal_fraction": 0.25,
            "info_type": "choice",
            "alternatives": ["read", "write"],
        },
    )
    assert reply["kind"] == "silence"
    opened = next(row for row in world.store.records(run_id) if row["kind"] == "ask_opened")
    assert opened["subgoal_fraction"] == 0.25 and opened["info_type"] == "choice"
    assert opened["alternatives"] == ["read", "write"] and "provenance" in opened
    assert world.store.total_spent(run_id) == 0


async def test_workspace_copy_and_async_ticket_have_actual_private_receipts(world):
    source = world.create_run(RunConfig())["run_id"]
    (world.store.run_dir(source) / "work" / "note.txt").write_text("actual content")
    await world.end_run(source)
    child = world.create_run(
        RunConfig(iota={"workspace": f"import({source})"}, wait_budget_s=0.001)
    )["run_id"]
    receipt = next(row for row in world.store.records(child) if row["kind"] == "workspace_import")
    assert receipt["source_record_head"] == world.store.verify_record(source)["head"]
    assert receipt["source_snapshot_root_hash"] == receipt["recipient_snapshot_root_hash"]
    assert receipt["bytes"] == len("actual content")
    ticket = await world.act(child, "wait", {"seconds": 0.02})
    assert ticket["kind"] == "ticket"
    recorded = next(row for row in world.store.records(child) if row["kind"] == "action_ticket")
    assert recorded["ticket_id"] == ticket["ticket_id"] and recorded["delivery"] == "asynchronous"
    await asyncio.gather(*world.jobs[child].values())


async def test_shuffled_transfer_control_is_explicit_and_does_not_rewrite_the_donor(world):
    source = world.create_run(RunConfig(stage=1, packs=["belief", "deliver"]))["run_id"]
    evidence = await world.act(source, "deliver", {"kind": "note", "body": "Source evidence"})
    for index, rank in enumerate((3, 5)):
        result = await world.act(
            source,
            "believe",
            {
                "record": {
                    "kind": "rule",
                    "claim": f"Strategy {index} remains worth testing.",
                    "rank": rank,
                    "evidence": [{"ref": evidence["action_id"], "class": "observed"}],
                }
            },
        )
        assert result["kind"] == "result", result
    await world.end_run(source)
    beliefs = world.services["beliefs"]
    before, source_records = beliefs.fold(source), world.store.records(source)
    child = world.create_run(
        RunConfig(
            stage=1,
            packs=["belief"],
            seed=7,
            belief_import_policy="shuffled",
            iota={"workspace": "fresh", "beliefs": f"import({source})"},
        )
    )["run_id"]
    mounted = beliefs.fold(child)
    assert {row["claim"] for row in mounted} == {row["claim"] for row in before}
    assert sorted(row["rank"] for row in mounted) == sorted(row["rank"] for row in before)
    assert any(
        row["rank"] != next(old["rank"] for old in before if old["id"] == row["id"])
        for row in mounted
    )
    assert all("initial_condition" not in row for row in mounted)
    assert all(
        event["belief"]["initial_condition"]["agent_endorsed"] is False
        for event in beliefs.beliefs(child)._records()
    )
    assert all(len(beliefs.beliefs(child).history(row["id"])) == 1 for row in mounted)
    assert (
        beliefs.checkpoint_fold(child, world.store.records(child)[-1]["seq"])["beliefs"] == mounted
    )
    assert beliefs.fold(source) == before and world.store.records(source) == source_records
    await world.end_run(child)
    assert world.store.verify_record(child)["valid"]
    imported = beliefs.export_continuity(child)
    assert beliefs.validate_bundle(imported, continuity=True)["valid"]
    rows_before = len(world.store.list_runs())
    with pytest.raises(ValueError, match="named authenticated donor"):
        world.create_run(RunConfig(stage=1, packs=["belief"], belief_import_policy="shuffled"))
    assert len(world.store.list_runs()) == rows_before


async def test_pilot_log_only_breakers_record_counterfactual_without_agent_intervention(world):
    run_id = world.create_run(RunConfig(breaker_policy={"mode": "log_only"}))["run_id"]
    for _ in range(8):
        await world.act(run_id, "wait", {"seconds": 0})
    await asyncio.sleep(0)
    assert world.store.get_run(run_id)["state"] == "running"
    records = world.store.records(run_id)
    logged = [row for row in records if row["kind"] == "breaker_would_intervene"]
    assert len(logged) == 1 and logged[0]["trigger"] == "identical_outcomes"
    assert not [row for row in records if row["kind"] == "intervention"]
    assert not world.store.inbox(run_id)["events"]


async def test_life_wake_condition_is_registered_without_identity_confound(world):
    configs = []
    for policy in ("time", "events", "time"):
        life = world.lives.create(
            {"stage": 1, "research_mode": "development", "wake_policy": policy}
        )
        started = await world.lives.start_session(life["life_id"], {})
        configs.append(world.run_config(started["run_id"]))
    assert configs[0]["tuple"]["wake_policy"] == "time"
    assert configs[1]["tuple"]["wake_policy"] == "events"
    assert configs[0]["tuple"]["life_protocol_id"] == configs[2]["tuple"]["life_protocol_id"]
    assert configs[0]["tuple"]["life_protocol_id"] != configs[1]["tuple"]["life_protocol_id"]


async def test_bridge_ignores_internal_accounting_and_private_archive_runs(world):
    visible = world.create_run(RunConfig())["run_id"]
    hidden = []
    for conditions in (
        {"world_accounting": True},
        {"private_archive": True},
        {"metering_lane": "judge"},
    ):
        row = world.store.create_run({"budget": {"usd": 1, "wall_s": 60}, **conditions})
        hidden.append(row["run_id"])
        world.store.update_run(row["run_id"], state="sealed")
    result = world.bridge()
    assert [row["run_id"] for row in result["runs"]] == [visible]
    assert all(world.bridge(run_id)["runs"] == [] for run_id in hidden)


async def test_deferred_observer_preserves_the_persisted_world_event_time(world, monkeypatch):
    run_id = world.create_run(RunConfig())["run_id"]
    event = world.store.push_event(run_id, {"type": "ask_resolved", "state": "rejected"})
    observations = []

    async def capture(selected, observation):
        observations.append((selected, observation))

    monkeypatch.setattr(world, "observe", capture)
    await asyncio.sleep(0.01)
    await world._observe_event(run_id, event)
    assert observations[0][0] == run_id
    assert observations[0][1]["observed_at"] == event["ts"]
    assert observations[0][1]["event_id"] == event["event_id"]


def test_browse_manifest_exposes_declared_site_tools_without_endpoint_override():
    tools = {row["name"]: row for row in manifest(["browse"])["tools"]}
    schema = tools["site_call"]["inputSchema"]
    assert set(schema["required"]) == {"session_id", "site_tool"}
    assert schema["additionalProperties"] is False
    assert "arguments" in schema["properties"] and "site_endpoint" in schema["properties"]


def test_implementation_identity_changes_with_packaged_code_and_ignores_private_data(tmp_path):
    source = tmp_path / "worldd"
    source.mkdir()
    (source / "runtime.py").write_text("first implementation")
    first = implementation_manifest(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "private.json").write_text("private runtime material")
    assert digest(implementation_manifest(tmp_path)) == digest(first)
    (source / "runtime.py").write_text("second implementation")
    assert digest(implementation_manifest(tmp_path)) != digest(first)


async def test_workspace_import_verifies_sealed_snapshot_before_creating_a_run(world):
    source = world.create_run(RunConfig())["run_id"]
    root = world.store.run_dir(source)
    (root / "work" / "note.txt").write_text("original artifact")
    await world.end_run(source)
    original_count = len(world.store.list_runs())
    (root / "work-snapshot" / "note.txt").write_text("changed artifact")
    with pytest.raises(ValueError, match="sealed content manifest"):
        world.create_run(RunConfig(iota={"workspace": f"import({source})"}))
    assert len(world.store.list_runs()) == original_count


@pytest.mark.parametrize("capture", [True, False])
async def test_export_uses_frozen_work_and_respects_model_capture_condition(world, capture):
    run_id = world.create_run(RunConfig(capture_model_io=capture))["run_id"]
    root = world.store.run_dir(run_id)
    (root / "work" / "artifact.txt").write_text("sealed artifact")
    (root / "model-io").mkdir(exist_ok=True)
    (root / "model-io" / "fixture.json").write_text('{"text":"consented fixture capture"}')
    await world.end_run(run_id)
    (root / "work" / "artifact.txt").write_text("changed after seal")
    with tarfile.open(fileobj=io.BytesIO(world.export(run_id)), mode="r:gz") as exported:
        names = exported.getnames()
        assert not any(name.startswith(f"{run_id}/work/") for name in names)
        assert (
            exported.extractfile(f"{run_id}/work-snapshot/artifact.txt").read()
            == b"sealed artifact"
        )
        assert (f"{run_id}/model-io/fixture.json" in names) is capture


async def test_research_browser_defaults_signed_after_life_inheritance(world, monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-trusted-control")
    monkeypatch.setattr(world.registry, "require_stage", lambda *args, **kwargs: None)
    research = world.create_run(RunConfig(stage=2, research_mode="research", packs=["browse"]))
    config = world.run_config(research["run_id"])
    assert config["browser_identity"] == config["tuple"]["browser_identity"] == "signed"
    assert "signed identity" in research["preamble"]
    assert research["run_token"] is None
    with pytest.raises(ValueError, match="preflight evidence"):
        world.activate_run(research["run_id"])
    unsigned = world.create_run(
        RunConfig(stage=2, research_mode="research", packs=["browse"], browser_identity="unsigned")
    )
    assert world.run_config(unsigned["run_id"])["tuple"]["browser_identity"] == "unsigned"


async def test_report_formats_are_actual_control_responses(tmp_path):
    app = create_app(tmp_path)
    world = app.state.world
    try:
        run_id = world.create_run(RunConfig())["run_id"]
        await world.end_run(run_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://world"
        ) as client:
            for format, media in [
                ("markdown", "text/markdown"),
                ("html", "text/html"),
                ("csv", "text/csv"),
            ]:
                response = await client.get(
                    "/v1/report", params={"run_id": run_id, "format": format}
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith(media)
                assert response.headers["cache-control"] == "no-store"
                assert run_id in response.text
            rejected = await client.get(
                "/v1/report", params={"run_id": run_id, "format": "unknown"}
            )
            assert rejected.status_code == 422
    finally:
        await world.aclose()
