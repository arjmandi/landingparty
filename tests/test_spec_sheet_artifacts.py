import json

from analysis.pipeline import PostRunPipeline
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.util import digest


class Gateway:
    async def aclose(self):
        pass


async def test_actual_delivered_self_assessment_is_s_and_frozen_fidelity_is_measured(tmp_path):
    world = create_app(tmp_path, gateway=Gateway()).state.world
    try:
        url, body = "https://example.org/frozen-fixture", b"unique fixture content"
        corpus = world.services["frozen"].import_corpus(
            {
                "cutoff": "2026-01-01T00:00:00+00:00",
                "captures": [
                    {
                        "url": url,
                        "captured_at": "2025-12-31T00:00:00+00:00",
                        "hash": digest(body),
                        "content_type": "text/plain",
                    }
                ],
                "search_lists": [
                    {"params": {"query": "unique fixture", "k": 10}, "items": [{"url": url}]}
                ],
            },
            {url: body},
        )
        source = world.create_run(
            RunConfig(mode="frozen", corpus_id=corpus["corpus_id"], frozen_at=corpus["cutoff"])
        )["run_id"]
        root = world.store.run_dir(source)
        (root / "work/out/self-assessment.json").write_text(
            json.dumps({"claim": "I believe I passed", "self_grade": 5})
        )
        await world.act(source, "fetch", {"url": url})
        await world.end_run(source)
        before = world.store.records(source)
        result = await PostRunPipeline(world).run(source)
        assert result["stages"]["checklist-self-assessment"]["status"] == "completed"
        assert result["stages"]["frozen-mitigation"]["status"] == "completed"
        report = world.report(source)
        assessment = report["spec_sheet"]["checklist_self_assessment"]
        assert assessment["column"] == "S" and assessment["headline"] is False
        assert assessment["assessment"]["self_grade"] == 5
        assert report["measurement_readings"]["checklist_self_assessment"]["column"] == "S"
        fidelity = report["spec_sheet"]["frozen_mitigation_fidelity"]
        assert fidelity["status"] == "measured" and fidelity["verified_captures"] == 1
        assert fidelity["fidelity"]["n"] == 1
        assert fidelity["manifest_hash"] == corpus["manifest_hash"]
        assert world.store.records(source) == before
    finally:
        await world.aclose()
