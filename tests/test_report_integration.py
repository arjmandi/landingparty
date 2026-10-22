"""Exercise the control plane's real sealing and analysis integration offline."""

import asyncio
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from worldd.app import create_app


class FixtureGateway:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    async def fetch(self, run_id, action_id, url, **kwargs):
        if url.endswith("/blocked"):
            return {
                "kind": "blocked",
                "type": "robots",
                "detail": "fixture refusal",
                "rule_id": "robots_disallow",
            }
        raw = b"<html><title>Fixture</title><p>Archived evidence.</p></html>"
        handle = f"/world/fetched/{action_id}.html"
        path = self.data_dir / "runs" / run_id / handle.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "kind": "result",
            "url": url,
            "final_url": url,
            "status": 200,
            "handle": handle,
            "bytes": len(raw),
            "hash": "sha256:fixture",
            "content_type": "text/html",
            "preview": "Archived evidence.",
        }

    async def aclose(self):
        pass


@pytest.fixture
async def report_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_BRAVE_API_KEY", "fixture-provider-key")

    async def provider(request):
        assert request.headers["host"] == "api.search.brave.com"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://example.org/source",
                            "title": "Source",
                            "description": "Fixture",
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as search:
        app = create_app(tmp_path, gateway=FixtureGateway(tmp_path), search_client=search)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://world.test"
            ) as client,
        ):
            yield app, client


async def act(client, run_id, tool, **params):
    response = await client.post(
        "/v1/act",
        json={
            "run_id": run_id,
            "tool": tool,
            "params": params,
        },
    )
    assert response.status_code == 200
    return response.json()


async def test_http_seal_saves_report_snapshot_export_and_real_spend(report_app):
    app, client = report_app
    created = await client.post("/v1/run", json={"packs": ["deliver"], "family": "F5"})
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert (await act(client, run_id, "fetch", url="https://example.org/source"))[
        "kind"
    ] == "result"
    assert (await act(client, run_id, "fetch", url="https://example.org/blocked"))[
        "type"
    ] == "robots"
    assert (await act(client, run_id, "search", query="public fixture"))["kind"] == "result"
    assert (await act(client, run_id, "ask", kind="message", text="A fixture message"))[
        "kind"
    ] == "silence"
    assert (await act(client, run_id, "deliver", kind="faq", body="Fixture artifact"))[
        "kind"
    ] == "silence"
    sealed = await client.post("/v1/beam-up", json={"run_id": run_id})
    assert sealed.status_code == 200
    assert sealed.json()["state"] == "sealed"
    response = await client.get("/v1/report", params={"run_id": run_id})
    assert response.status_code == 200
    report = response.json()
    assert report["sealed"]
    assert report["axes"] == {"usd": 0.005, "world_actions": 5}
    assert report["activity"]["asks_by_kind"] == {"message": 1}
    assert report["activity"]["blocks"] == {"robots": 1}
    assert report["integrity"]["record"]["valid"]
    assert report["integrity"]["ledger"]["valid"]
    assert report["completeness"]["verdict"] == "unknown"
    assert report["tuple"] == created.json()["tuple"]
    assert report["columns"]["J"]["status"] == "unmeasured"
    assert report["columns"]["S"]["status"] == "unmeasured"
    root = app.state.store.run_dir(run_id)
    assert json.loads((root / "report.json").read_text()) == report
    assert (root / "spec-sheet.md").is_file()
    exported = await client.get("/v1/export", params={"run_id": run_id})
    assert exported.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(exported.content), mode="r:gz") as archive:
        names = archive.getnames()
        assert f"{run_id}/report.json" in names
        assert f"{run_id}/spec-sheet.md" in names
        assert any("/work-snapshot/out/" in name for name in names)
        assert not any("model-io" in name or "vault" in name for name in names)
        record = archive.extractfile(f"{run_id}/record.jsonl").read().decode()
        assert "fixture-provider-key" not in record
        assert json.loads(record.splitlines()[-1])["kind"] == "seal"


async def test_batch_resources_and_blocks_are_counted_in_report(report_app):
    app, client = report_app
    created = await client.post("/v1/run", json={})
    run_id = created.json()["run_id"]
    ticket = await act(
        client,
        run_id,
        "fetch_many",
        urls=[
            "https://example.org/one",
            "https://example.org/two",
            "https://example.org/blocked",
        ],
    )
    assert ticket["kind"] == "ticket"
    tasks = list(app.state.world.jobs.get(run_id, {}).values())
    await asyncio.gather(*tasks)
    response = await client.post("/v1/beam-up", json={"run_id": run_id})
    assert response.status_code == 200
    report = (await client.get("/v1/report", params={"run_id": run_id})).json()
    assert report["axes"]["world_actions"] == 1
    assert report["activity"]["fetched_resources"] == 2
    assert report["activity"]["blocks"] == {"robots": 1}
    assert report["activity"]["distinct_content_hashes"] == 1
    assert report["activity"]["actions_without_outcome"] == 0


async def test_open_reservation_prevents_a_clean_seal_report(report_app):
    app, client = report_app
    created = await client.post("/v1/run", json={})
    run_id = created.json()["run_id"]
    reservation = app.state.store.reserve(run_id, "0.02")
    assert reservation
    await client.post("/v1/beam-up", json={"run_id": run_id})
    report = (await client.get("/v1/report", params={"run_id": run_id})).json()
    assert report["integrity"]["ledger"]["valid"] is False
    assert report["integrity"]["tainted"] is True
    assert report["axes"]["usd"] == 0
