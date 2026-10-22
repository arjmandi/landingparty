"""Exercise stage-aware life transitions through the actual HTTP application."""

import asyncio
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.security import create_agent_app


@pytest.fixture
async def full_app(tmp_path):
    async def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            text="<h1>World evidence</h1><p>Version one.</p>",
            headers={"content-type": "text/html"},
        )

    async def resolve(host, port):
        return ["93.184.216.34"]

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    gateway = Gateway(
        tmp_path,
        {"domain_interval_s": 0, "domain_burst": 100, "global_requests_per_minute": 1000},
        client=upstream,
        resolver=resolve,
    )
    app = create_app(tmp_path, gateway=gateway)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://world"
    )
    yield app, client
    await client.aclose()
    await app.state.world.aclose()
    await upstream.aclose()


async def new_life(client, **values):
    response = await client.post(
        "/v1/lives", json={"stage": 1, "research_mode": "development", **values}
    )
    assert response.status_code == 201, response.text
    return response.json()["life_id"]


async def new_session(client, life_id, **values):
    response = await client.post(f"/v1/lives/{life_id}/sessions", json=values)
    assert response.status_code == 201, response.text
    return response.json()


async def act(client, run_id, tool, **params):
    response = await client.post("/v1/act", json={"run_id": run_id, "tool": tool, "params": params})
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for_sleep(world, life_id):
    task = world.services["lives"].tasks[life_id + ":sleep"]
    await asyncio.wait_for(asyncio.shield(task), 2)


async def test_actual_life_export_preserves_event_chain_snapshots_and_accounting_metadata(full_app):
    app, client = full_app
    world = app.state.world
    life_id = await new_life(client)
    created = await new_session(client, life_id, packs=["deliver"])
    run_id = created["run_id"]
    original = world.store.run_dir(run_id) / "work" / "artifact.txt"
    original.write_text("Frozen agent artifact")
    with pytest.raises(ValueError, match="Seal active"):
        world.services["lives"].export(life_id)
    assert (await client.post("/v1/beam-up", json={"run_id": run_id})).status_code == 200
    original.write_text("Mutable local researcher edit after seal")
    assert (await client.post(f"/v1/lives/{life_id}/end", json={})).status_code == 200
    body = world.services["lives"].export(life_id)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        manifest = json.load(archive.extractfile(f"{life_id}/manifest.json"))
        assert (
            manifest["verification"]["valid"] and manifest["verification"]["materialized_verified"]
        )
        assert not manifest["partial"]
        accounting = json.load(archive.extractfile(f"{life_id}/accounting/runs.json"))
        assert accounting[0]["run_id"] == run_id and accounting[0]["ledger"]["valid"]
        nested = archive.extractfile(f"{life_id}/sessions/{run_id}.tar.gz").read()
        with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as session:
            assert (
                session.extractfile(f"{run_id}/work-snapshot/artifact.txt").read()
                == b"Frozen agent artifact"
            )
            assert not any(
                "grant-signing.key" in name or "/vault/" in name for name in session.getnames()
            )


async def test_actual_incident_is_anchored_to_life_without_mutating_other_lives(full_app):
    app, client = full_app
    first, other = await new_life(client), await new_life(client)
    identity = await client.post(
        "/v1/identities",
        json={
            "life_id": first,
            "name": "fixture-life",
            "principal": "researcher",
            "mailbox": "fixture@example.org",
            "homepage": "https://example.org/fixture",
        },
    )
    assert identity.status_code == 201, identity.text
    created = await new_session(client, first)
    response = await client.post(
        "/v1/incidents",
        json={
            "run_id": created["run_id"],
            "platform": "fixture",
            "incident_kind": "complaint",
            "owner": "researcher",
            "detail": "Actual operator-recorded platform complaint",
        },
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["item"]["item_id"]
    lives = app.state.world.services["lives"]
    assert lives.get(first)["incidents"][0]["incident_ref"] == item_id
    assert lives.get(other)["incidents"] == []
    lives.record_incident(first, item_id)
    assert len(lives.get(first)["incidents"]) == 1
    with pytest.raises(ValueError, match="does not belong"):
        lives.record_incident(other, item_id)
    assert lives.verify(first)["valid"]


async def test_inbound_during_sleep_seal_handoff_reaches_next_context_once(full_app):
    app, client = full_app
    world = app.state.world
    life_id = await new_life(client)
    source = (await new_session(client, life_id, packs=["schedule"]))["run_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = world.end_run

    async def delayed_end(run_id, **options):
        if run_id == source and not release.is_set():
            entered.set()
            await release.wait()
        return await original(run_id, **options)

    world.end_run = delayed_end
    try:
        assert (await act(client, source, "sleep", wake_on=["message_received"]))[
            "kind"
        ] == "silence"
        await asyncio.wait_for(entered.wait(), 1)
        assert world.store.get_run(source)["state"] == "running"
        world.push(source, {"type": "message_received", "body": "First pre-seal handoff"})
        world.push(source, {"type": "message_received", "body": "Second while waking"})
        release.set()
        await wait_for_sleep(world, life_id)
        next_run = (await new_session(client, life_id))["run_id"]
        messages = [
            event["body"]
            for event in world.store.inbox(next_run)["events"]
            if event["type"] == "message_received"
        ]
        assert messages == ["First pre-seal handoff", "Second while waking"]
        assert world.store.records(source)[-1]["kind"] == "seal"
        assert world.store.verify_record(source)["valid"]
    finally:
        release.set()
        world.end_run = original


async def test_stasis_handoff_rebuilds_queue_after_inbox_commit_without_late_commit(full_app):
    app, client = full_app
    world = app.state.world
    lives = world.services["lives"]
    life_id = await new_life(client)
    source = (await new_session(client, life_id, packs=["schedule"]))["run_id"]
    world.push(source, {"type": "message_received", "body": "Already seen before stasis"})
    entered, release = asyncio.Event(), asyncio.Event()
    original_end, original_append = world.end_run, world.late_events.append

    async def delayed_end(run_id, **options):
        if run_id == source and not release.is_set():
            entered.set()
            await release.wait()
        return await original_end(run_id, **options)

    def interrupted_queue(*args, **kwargs):
        raise OSError("crash after authoritative inbox commit")

    world.end_run = delayed_end
    try:
        await act(client, source, "sleep", wake_on=["message_received"])
        await asyncio.wait_for(entered.wait(), 1)
        world.late_events.append = interrupted_queue
        with pytest.raises(OSError):
            world.push(source, {"type": "message_received", "body": "Durable handoff body"})
        world.late_events.append = original_append
        assert world.late_events.pending(life_id) == []
        assert lives.get(life_id)["state"] == "sleeping"
        release.set()
        await wait_for_sleep(world, life_id)
        await lives.recover()
        await lives.recover()
        assert lives.get(life_id)["state"] == "waking"
        assert len(world.late_events.pending(life_id)) == 1
        target = (await new_session(client, life_id))["run_id"]
        messages = [
            event["body"]
            for event in world.store.inbox(target)["events"]
            if event["type"] == "message_received"
        ]
        assert messages == ["Durable handoff body"]
        assert world.store.verify_record(source)["valid"]
    finally:
        release.set()
        world.end_run, world.late_events.append = original_end, original_append


async def test_forward_retry_after_lost_delivery_ack_does_not_duplicate_inbox_or_record(full_app):
    app, client = full_app
    world = app.state.world
    life_id = await new_life(client)
    source = (await new_session(client, life_id, packs=["schedule"]))["run_id"]
    await act(client, source, "sleep", wake_on=["message_received"])
    await wait_for_sleep(world, life_id)
    queued = world.push(source, {"type": "message_received", "body": "One actual response"})
    target = (await new_session(client, life_id))["run_id"]
    original = world.store.inbox(target)
    records = world.store.records(target)
    world.late_events.db.execute(
        "UPDATE events SET delivered_to=NULL WHERE event_id=?", (queued["event_id"],)
    )
    for event in world.late_events.pending(life_id):
        world.push(
            target,
            {
                **event,
                "source_run_id": event["run_id"],
                "late_event_id": event["event_id"],
                "observed_at": event["ts"],
            },
        )
        world.late_events.delivered(event["event_id"], target)
    assert world.store.inbox(target) == original
    assert world.store.records(target) == records
    assert world.late_events.pending(life_id) == []
    assert world.store.verify_record(target)["valid"]


async def test_real_sleep_wake_restores_workspace_and_registered_tools(full_app):
    app, client = full_app
    world = app.state.world
    life_id = await new_life(client)
    first = await new_session(client, life_id, packs=["belief", "schedule", "deliver"])
    old_id = first["run_id"]
    note = world.store.run_dir(old_id) / "work" / "remember.txt"
    note.write_text("Retain across a context reset.")
    reply = await act(client, old_id, "sleep", wake_on=["message_received"])
    assert reply["kind"] == "silence"
    await wait_for_sleep(world, life_id)
    old_records = world.store.records(old_id)
    assert old_records[-1]["kind"] == "seal"
    assert world.services["lives"].get(life_id)["state"] == "sleeping"
    world.push(old_id, {"type": "message_received", "body": "A delayed reply"})
    second = await new_session(client, life_id)
    new_id = second["run_id"]
    assert (world.store.run_dir(new_id) / "work" / "remember.txt").read_text() == note.read_text()
    assert {tool["name"] for tool in second["manifest"]["tools"]} >= {"believe", "sleep", "deliver"}
    events = world.store.inbox(new_id)["events"]
    assert any(event["type"] == "wake" for event in events)
    assert any(event["type"] == "message_received" for event in events)
    assert world.store.records(old_id) == old_records
    assert world.store.verify_record(new_id)["valid"]


async def test_context_reset_preserves_low_rank_beliefs(full_app):
    app, client = full_app
    world = app.state.world
    life_id = await new_life(client)
    first = await new_session(client, life_id, packs=["belief", "schedule"])
    run_id = first["run_id"]
    fetched = await act(client, run_id, "fetch", url="https://example.com/evidence")
    action_id = Path(fetched["handle"]).stem
    belief = await act(
        client,
        run_id,
        "believe",
        record={
            "kind": "fact",
            "claim": "Version one is present.",
            "rank": 2,
            "triple": {"subject": "example", "predicate": "version", "object": "one"},
            "evidence": [{"ref": action_id, "class": "observed"}],
        },
    )
    assert belief["kind"] == "result", belief
    original_id = belief["data"]["id"]
    await act(client, run_id, "sleep", wake_on=["message_received"])
    await wait_for_sleep(world, life_id)
    world.push(run_id, {"type": "message_received", "body": "wake"})
    second = await new_session(client, life_id)
    folded = world.services["beliefs"].fold(second["run_id"])
    assert any(record["id"] == original_id and record["rank"] == 2 for record in folded)


async def test_life_budget_is_enforced_across_real_sessions(full_app):
    app, client = full_app
    life_id = await new_life(client, budget={"usd_life": 1, "wall_life_days": 28})
    first = await new_session(client, life_id, budget={"usd": 1, "wall_s": 30})
    assert app.state.store.debit(first["run_id"], "compute", "0.6")
    response = await client.post("/v1/beam-up", json={"run_id": first["run_id"]})
    assert response.status_code == 200
    second = await new_session(client, life_id, budget={"usd": 10, "wall_s": 30})
    assert app.state.world.run_config(second["run_id"])["budget"]["usd"] == 0.4
    assert app.state.store.debit(second["run_id"], "compute", "0.4")
    assert not app.state.store.debit(second["run_id"], "compute", "0.000000001")


async def test_stage_three_development_does_not_unlock_research(full_app, monkeypatch):
    app, client = full_app
    created = await client.post("/v1/run", json={"stage": 3, "research_mode": "development"})
    assert created.status_code == 201, created.text
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "researcher-fixture-token")
    refused = await client.post(
        "/v1/run",
        json={"stage": 1, "research_mode": "research"},
        headers={"x-world-control-token": "researcher-fixture-token"},
    )
    assert refused.status_code == 400
    assert "registered evidence" in refused.json()["detail"]
    assert not app.state.world.services["registry"].require_stage(3, "development")[
        "research_authorized"
    ]


async def test_clock_wake_uses_life_policy_through_http(full_app):
    app, client = full_app
    world = app.state.world
    now = datetime.now(UTC)
    lives = world.services["lives"]
    lives.clock = lambda: now
    life_id = await new_life(client, wake_policy="time")
    first = await new_session(client, life_id, packs=["schedule"])
    await act(client, first["run_id"], "sleep", until=(now + timedelta(hours=1)).isoformat())
    await wait_for_sleep(world, life_id)
    early = await client.post(f"/v1/lives/{life_id}/sessions", json={})
    assert early.status_code == 400
    now += timedelta(hours=1)
    await lives.tick(life_id)
    second = await new_session(client, life_id)
    events = world.store.inbox(second["run_id"])["events"]
    assert any(event["type"] == "wake" and event["reason"] == "time" for event in events)


async def test_failed_import_does_not_leave_uninitialized_running_episode(full_app):
    app, client = full_app
    response = await client.post("/v1/run", json={"iota": {"workspace": "import(r_missing)"}})
    assert response.status_code in {400, 404}, response.text
    assert not any(run["state"] == "running" for run in app.state.store.list_runs())


async def test_kernel_failure_does_not_leave_untracked_inflight_job(full_app, monkeypatch):
    app, client = full_app
    world = app.state.world
    created = await client.post("/v1/run", json={"stage": 1, "packs": ["belief"]})
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]

    async def broken_observer(*args, **kwargs):
        raise RuntimeError("kernel failure")

    monkeypatch.setattr(world.services["beliefs"], "observe_event", broken_observer)
    response = await client.post(
        "/v1/act",
        json={"run_id": run_id, "tool": "fetch", "params": {"url": "https://example.com/evidence"}},
    )
    assert response.status_code == 200, response.text
    assert world.status(run_id)["in_flight"] == 0
    assert world.store.get_run(run_id)["tainted"] is True


async def test_agent_listener_exposes_only_its_own_run_and_revokes_at_seal(full_app):
    app, client = full_app
    first = (await client.post("/v1/run", json={})).json()
    second = (await client.post("/v1/run", json={})).json()
    agent = create_agent_app(app.state.world)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(agent),
        base_url="http://agent",
        headers={"authorization": "Bearer " + first["run_token"]},
    ) as player:
        assert (
            await player.get("/v1/status", params={"run_id": first["run_id"]})
        ).status_code == 200
        assert (
            await player.get("/v1/status", params={"run_id": second["run_id"]})
        ).status_code == 401
        assert (await player.get("/v1/lives")).status_code == 404
        await client.post("/v1/beam-up", json={"run_id": first["run_id"]})
        assert (
            await player.get("/v1/status", params={"run_id": first["run_id"]})
        ).status_code == 401


async def test_rehearsed_individual_grant_updates_manifest_and_consumes_once(full_app):
    app, client = full_app
    world = app.state.world
    source = (await client.post("/v1/run", json={})).json()["run_id"]
    await act(client, source, "fetch", url="https://example.com/rehearsal-source")
    await client.post("/v1/beam-up", json={"run_id": source})
    target = (await client.post("/v1/run", json={})).json()["run_id"]
    proposed = await client.post(
        "/v1/grants",
        json={
            "run_id": target,
            "tools": ["deliver"],
            "action_classes": ["write_sandbox"],
            "purpose": "One local deliverable",
            "archive_in": source,
        },
    )
    assert proposed.status_code == 201, proposed.text
    grant_id = proposed.json()["grant_id"]
    for _ in range(10):
        rehearsed = await client.post(
            f"/v1/grants/{grant_id}/rehearse",
            json={"action_class": "write_sandbox", "url": "https://example.com/rehearsal-source"},
        )
        assert rehearsed.status_code == 200 and rehearsed.json()["kind"] == "result", rehearsed.text
        assert rehearsed.json()["proxy_receipt"]["archive_id"] == source
    activated = await client.post(
        f"/v1/grants/{grant_id}/activate",
        json={
            "verdict": {
                "decision": "pass",
                "reviewer": "captain",
                "rehearsal_hash": world.grants.rehearsal_hash(grant_id),
            }
        },
    )
    assert activated.status_code == 200, activated.text
    names = {
        tool["name"]
        for tool in (await client.get("/v1/manifest", params={"run_id": target})).json()["tools"]
    }
    assert "deliver" in names and "believe" not in names
    result = await act(client, target, "deliver", kind="note", body="A permitted local artifact")
    assert result["kind"] == "silence"
    after = (await client.get("/v1/manifest", params={"run_id": target})).json()
    assert "deliver" not in {tool["name"] for tool in after["tools"]}
    assert (await act(client, target, "deliver", kind="note", body="Second attempt"))[
        "kind"
    ] == "blocked"
    await client.post("/v1/beam-up", json={"run_id": target})
    assert world.store.verify_record(target)["valid"]
    assert world.grants.verify(target)["valid"]
    assert world.store.records(target)[-1]["kind"] == "seal"
