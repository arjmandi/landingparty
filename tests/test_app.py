"""End-to-end control-plane tests with real policy, archive and persistence."""

import asyncio
import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway


@pytest.fixture
async def app_fixture(tmp_path):
    calls = []
    release = asyncio.Event()

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.path == "/slow":
            await release.wait()
        return httpx.Response(
            200,
            text="<h1>Archived evidence</h1><p>The value is 42.</p>",
            headers={"content-type": "text/html"},
        )

    async def resolver(host, port):
        return ["93.184.216.34"]

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = Gateway(
        tmp_path,
        {"domain_interval_s": 0, "domain_burst": 100, "global_requests_per_minute": 1000},
        client=upstream,
        resolver=resolver,
    )
    app = create_app(tmp_path, gateway=gateway)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://world")
    yield app, client, calls, release
    await client.aclose()
    await app.state.world.aclose()
    await upstream.aclose()


async def start(client, **config):
    response = await client.post("/v1/run", json=config)
    assert response.status_code == 201, response.text
    return response.json()


async def act(client, run_id, tool, **params):
    return (
        await client.post("/v1/act", json={"run_id": run_id, "tool": tool, "params": params})
    ).json()


async def test_full_archive_replay_and_export_workflow(app_fixture):
    app, client, calls, _release = app_fixture
    info = await start(client, packs=["deliver"])
    run_id = info["run_id"]
    fetched = await act(client, run_id, "fetch", url="https://example.com/article", inline=True)
    assert fetched["kind"] == "result" and "42" in fetched["inline"]
    extracted = await act(client, run_id, "extract", handle=fetched["handle"], view="text")
    assert extracted["data"] == "Archived evidence\nThe value is 42."
    await act(client, run_id, "deliver", kind="faq", body="The answer is 42.")
    await client.post("/v1/beam-up", json={"run_id": run_id})
    records = app.state.store.records(run_id)
    assert sum(r["kind"] == "beam_down" for r in records) == 1
    assert records[-1]["kind"] == "seal"
    assert app.state.store.verify_record(run_id)["valid"]
    exported = await client.get("/v1/export", params={"run_id": run_id})
    with tarfile.open(fileobj=io.BytesIO(exported.content), mode="r:gz") as archive:
        names = archive.getnames()
        assert any("work-snapshot/out/" in name for name in names)
        assert any(name.endswith("archive.warc.gz") for name in names)
        assert any(name.endswith("spec-sheet.md") for name in names)
    before = len(calls)
    replay = await start(client, mode="replay", archive_in=run_id)
    found = await act(client, replay["run_id"], "fetch", url="https://example.com/article")
    missing = await act(client, replay["run_id"], "fetch", url="https://example.com/uncaptured")
    assert found["provenance"]["capture_mode"] == "replay"
    assert missing["type"] == "replay_miss"
    assert len(calls) == before


async def test_timeout_ticket_keeps_job_and_delivers_to_long_poll(app_fixture):
    _app, client, _calls, release = app_fixture
    run_id = (await start(client, wait_budget_s=0.01))["run_id"]
    ticket = await act(client, run_id, "fetch", url="https://example.com/slow")
    assert ticket["kind"] == "ticket"
    polling = asyncio.create_task(
        client.get(
            "/v1/inbox",
            params={
                "run_id": run_id,
                "wait_seconds": 1,
            },
        )
    )
    release.set()
    response = await polling
    events = response.json()["events"]
    assert events[0]["action_id"] == ticket["action_id"]
    assert events[0]["reply"]["kind"] == "result"


async def test_max_in_flight_refuses_without_adding_action(app_fixture):
    app, client, _calls, _release = app_fixture
    run_id = (await start(client, max_in_flight=1))["run_id"]
    first = await act(client, run_id, "fetch", url="https://example.com/slow", wait=False)
    second = await act(client, run_id, "fetch", url="https://example.com/second", wait=False)
    assert first["kind"] == "ticket"
    assert second["type"] == "too_many_in_flight"
    assert sum(r["kind"] == "action" for r in app.state.store.records(run_id)) == 1
    assert (await act(client, run_id, "status"))["kind"] == "state"


async def test_wall_deadline_ends_run_and_cancels_wait(app_fixture):
    app, client, _calls, _release = app_fixture
    run_id = (await start(client, budget={"usd": 1, "wall_s": 0.03}))["run_id"]
    ticket = asyncio.create_task(act(client, run_id, "wait", seconds=20))
    reply = await asyncio.wait_for(ticket, 1)
    assert reply["rule_id"] == "run_cancelled"
    for _ in range(30):
        if app.state.store.get_run(run_id)["state"] == "sealed":
            break
        await asyncio.sleep(0.01)
    assert app.state.store.get_run(run_id)["end"]["truncated"] == "wall_clock"


async def test_budget_hiding_and_operator_policy(app_fixture):
    _app, client, _calls, _release = app_fixture
    info = await start(client, budget_visible=False)
    status = await act(client, info["run_id"], "status")
    assert "spent" not in status and "cap" not in status
    assert "Status shows" not in info["preamble"]
    rejected = await client.post(
        "/v1/bridge/message", json={"run_id": info["run_id"], "text": "hi"}
    )
    assert rejected.status_code == 400
    allowed = await start(
        client, reply_policy="blockers_plus_checkins", operator_messages_allowed=True
    )
    response = await client.post(
        "/v1/bridge/message",
        json={
            "run_id": allowed["run_id"],
            "text": "world observation",
            "human_seconds": 2,
        },
    )
    assert response.json()["type"] == "operator_message"


async def test_pack_gates_and_invalid_requests(app_fixture):
    _app, client, _calls, _release = app_fixture
    run_id = (await start(client))["run_id"]
    assert (await act(client, run_id, "believe", claim="unsupported"))["rule_id"] == "tool_set"
    assert (await act(client, run_id, "deliver", kind="text", body="x"))["rule_id"] == "tool_set"
    assert (await act(client, run_id, "fetch", url="https://example.com", method="POST"))[
        "rule_id"
    ] == "invalid_params"
    response = await client.post("/v1/run", json={"isolation_profile": "fenced"})
    assert response.status_code == 201
    fenced_id = response.json()["run_id"]
    preflight = next(r for r in _app.state.store.records(fenced_id) if r["kind"] == "preflight")
    assert preflight["checks"]["fenced_egress"] == "unverified"
    assert (await client.post("/v1/run", json={"packs": ["belief"]})).status_code == 422
    response = await client.get("/v1/status", params={"run_id": "r_unknown"})
    assert response.status_code == 404


async def test_search_archive_and_budget_stop(app_fixture, monkeypatch):
    app, client, _calls, _release = app_fixture
    monkeypatch.setenv("WORLD_BRAVE_API_KEY", "test-only-key")
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Evidence", "url": "https://example.com", "description": "A fact"}
                    ]
                }
            },
        )

    search = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    await app.state.world.search_client.aclose()
    app.state.world.search_client = search
    run_id = (await start(client, budget={"usd": 0.005, "wall_s": 60}))["run_id"]
    result = await act(client, run_id, "search", query="fact")
    assert result["items"][0]["rank"] == 1
    assert app.state.store.spend(run_id) == {"search": 0.005}
    await asyncio.sleep(0)
    await app.state.world.end_run(run_id)
    assert app.state.store.get_run(run_id)["end"]["reason"] == "budget_exhausted"
    replay = (await start(client, mode="replay", archive_in=run_id))["run_id"]
    found = await act(client, replay, "search", query="fact")
    assert found["provider_id"] == f"local@{run_id}"
    assert len(requests) == 1
    assert app.state.store.total_spent(replay) == 0


async def test_export_does_not_follow_workspace_symlinks(app_fixture, tmp_path):
    _app, client, _calls, _release = app_fixture
    info = await start(client)
    secret = tmp_path / "outside.txt"
    secret.write_text("external sentinel")
    from pathlib import Path

    (Path(info["workspace_path"]) / "outside-link").symlink_to(secret)
    await client.post("/v1/beam-up", json={"run_id": info["run_id"]})
    response = await client.get("/v1/export", params={"run_id": info["run_id"]})
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        assert all("outside-link" not in name for name in archive.getnames())
        assert all(
            b"external sentinel" not in archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        )


async def test_effective_policy_and_price_identity(app_fixture):
    app, client, _calls, _release = app_fixture
    from worldd.lane import price_table_id
    from worldd.util import digest

    info = await start(client)
    assert info["tuple"]["policy_id"] == digest(app.state.world.gateway.policy)
    assert info["tuple"]["price_table_id"] == price_table_id()
    assert info["tuple"]["classifier_id"] == app.state.world.gateway.classifier_id
    assert info["tuple"]["extractor_id"] == "beautifulsoup4-v1"


async def test_private_arguments_are_redacted_before_recording(app_fixture):
    app, client, _calls, _release = app_fixture
    run_id = (await start(client))["run_id"]
    await act(
        client,
        run_id,
        "fetch",
        url="https://example.com",
        headers={"Authorization": "sentinel-key"},
    )
    records = app.state.store.records(run_id)
    assert "sentinel-key" not in json.dumps(records)
    action = next(r for r in records if r["kind"] == "action")
    assert action["params"]["redacted"] is True


async def test_workspace_special_files_do_not_prevent_sealing(app_fixture):
    app, client, _calls, _release = app_fixture
    info = await start(client)
    os.mkfifo(Path(info["workspace_path"]) / "agent-pipe")
    response = await client.post("/v1/beam-up", json={"run_id": info["run_id"]})
    assert response.status_code == 200 and response.json()["state"] == "sealed"
    records = app.state.store.records(info["run_id"])
    omission = next(r for r in records if r["kind"] == "snapshot_omissions")
    assert omission["paths"] == ["agent-pipe"]


async def test_seal_detects_corrupt_record_mirror(app_fixture):
    app, client, _calls, _release = app_fixture
    info = await start(client)
    run_id = info["run_id"]
    record = app.state.store.run_dir(run_id) / "record.jsonl"
    record.write_text(record.read_text().replace('"beam_down"', '"tampered"'))
    response = await client.post("/v1/beam-up", json={"run_id": run_id})
    assert response.status_code == 200 and response.json()["tainted"] is True
    assert not app.state.store.verify_record(run_id)["valid"]
