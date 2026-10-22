import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from worldd.gateway import canonical_url
from worldd.replay import FrozenCorpusService, ReplayIndex
from worldd.storage import Store
from worldd.util import digest


@pytest.fixture
def replay(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    index = ReplayIndex(world)
    world.services["replay"] = index
    frozen = FrozenCorpusService(world)
    yield world, index, frozen
    index.db.close()
    store.close()


def archive(world):
    store = world.store
    run_id = store.create_run({"budget": {"usd": 1}})["run_id"]
    root = store.run_dir(run_id)
    (root / "world" / "fetched").mkdir(parents=True)
    body = b"<html><title>Harbour status</title><p>Harbour vessel schedules and tidal observations.</p></html>"
    (root / "world" / "fetched" / "a_page.html").write_bytes(body)
    index = {
        canonical_url("https://example.com/harbour"): {
            "reply": {
                "kind": "result",
                "url": "https://example.com/harbour",
                "final_url": "https://example.com/harbour",
                "hash": digest(body),
                "content_type": "text/html",
            },
            "body_path": "world/fetched/a_page.html",
        }
    }
    (root / "archive-index.json").write_text(json.dumps(index))
    items = [
        {
            "rank": 1,
            "url": "https://example.com/harbour",
            "title": "Harbour",
            "snippet": "Harbour timetable",
        },
        {
            "rank": 2,
            "url": "https://other.com/tides",
            "title": "Harbour tides",
            "snippet": "Harbour tides and sea levels",
        },
    ]
    store.append_record(
        run_id, "action", action_id="a_search", tool="search", params={"query": "harbour", "k": 10}
    )
    store.append_record(
        run_id, "outcome", action_id="a_search", reply={"kind": "result", "items": items}
    )
    return run_id


def test_index_searches_new_queries_from_real_bodies_and_measures_archived_overlap(replay):
    world, index, _ = replay
    source = archive(world)
    built = index.build(source)
    assert built["documents"] == 2 and built["queries"] == 1
    assert built["fidelity"]["status"] == "measured" and built["fidelity"]["mean"] == 1
    assert built["fidelity"]["n"] == 1
    results = index.search(source, "vessel schedules")
    assert results["provider_id"] == f"local@{source}"
    assert results["items"][0]["url"] == "https://example.com/harbour"
    assert "vessel schedules" in results["items"][0]["snippet"]
    assert (
        index.search(source, "harbour", site="other.com")["items"][0]["url"]
        == "https://other.com/tides"
    )
    assert index.search(source, "nosuchthing")["type"] == "replay_miss"


def test_local_query_cannot_inject_fts_or_sql(replay):
    world, index, _ = replay
    source = archive(world)
    index.build(source)
    assert index.search(source, '"; DROP TABLE replay_fts; -- harbour')["kind"] == "result"
    assert index.search(source, "harbour")["items"]


def test_corrupt_resource_is_not_indexed_as_verified_body(replay):
    world, index, _ = replay
    source = archive(world)
    (world.store.run_dir(source) / "world/fetched/a_page.html").write_text("replaced body")
    built = index.build(source)
    assert built["errors"][0]["reason"] == "resource_hash_mismatch"
    assert index.search(source, "replaced body")["kind"] == "blocked"
    assert index.search(source, "harbour")["kind"] == "result"  # authentic archived snippet remains


def test_canary_archive_is_excluded_from_shared_retrieval(replay):
    world, index, _ = replay
    source = world.store.create_run({"budget": {"usd": 1}, "canary": "replay_only"})["run_id"]
    with pytest.raises(ValueError, match="canary"):
        index.build(source)


@pytest.mark.parametrize(
    "private",
    [
        {"canary": "replay_only"},
        {"private_archive": True},
        {"world_accounting": True},
    ],
)
def test_shared_index_rejects_every_private_measurement_and_replay_generation(replay, private):
    world, index, _ = replay
    root = world.store.create_run({"budget": {"usd": 1}, **private})["run_id"]
    first = world.store.create_run({"budget": {"usd": 1}, "measurement_of": root})["run_id"]
    second = world.store.create_run({"budget": {"usd": 1}, "measurement_of": first})["run_id"]
    playback = world.store.create_run(
        {"budget": {"usd": 1}, "archive_in": second, "mode": "replay"}
    )["run_id"]
    for run_id in (root, first, second, playback):
        assert index.indexable(run_id) is False
        with pytest.raises(ValueError, match="private or canary"):
            index.build(run_id)
        assert index.search(run_id, "hidden canary")["rule_id"] == "replay_private_archive"
    assert index.db.execute("SELECT COUNT(*) FROM replay_manifests").fetchone()[0] == 0


def test_indexability_checks_both_ancestry_edges_and_accepts_clean_shared_ancestors(replay):
    world, index, _ = replay
    root = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    left = world.store.create_run({"budget": {"usd": 1}, "measurement_of": root})["run_id"]
    right = world.store.create_run({"budget": {"usd": 1}, "archive_in": root})["run_id"]
    diamond = world.store.create_run(
        {"budget": {"usd": 1}, "measurement_of": left, "archive_in": right}
    )["run_id"]
    assert index.indexable(diamond) and index.build(diamond)["documents"] == 0
    private = world.store.create_run({"budget": {"usd": 1}, "private_archive": True})["run_id"]
    mixed = world.store.create_run(
        {"budget": {"usd": 1}, "measurement_of": root, "archive_in": private}
    )["run_id"]
    assert index.indexable(mixed) is False


def test_cyclic_missing_and_malformed_ancestry_fail_closed(replay):
    world, index, _ = replay
    first = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    second = world.store.create_run({"budget": {"usd": 1}, "measurement_of": first})["run_id"]
    # Deliberately corrupt immutable private test rows to exercise defensive cycle detection.
    with world.store._lock:
        world.store.db.execute(
            "UPDATE runs SET config=? WHERE run_id=?",
            (json.dumps({"budget": {"usd": 1}, "archive_in": second}), first),
        )
    assert index.indexable(first) is False and index.indexable(second) is False
    for parent in ("r_missing", "invalid run id", [first]):
        run_id = world.store.create_run({"budget": {"usd": 1}, "measurement_of": parent})["run_id"]
        assert index.indexable(run_id) is False


def test_no_search_history_is_unmeasured_fidelity(replay):
    world, index, _ = replay
    source = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    manifest = index.build(source)
    assert manifest["fidelity"]["status"] == "unmeasured"
    assert manifest["fidelity"]["n"] == 0


def corpus(frozen):
    body = b'{"harbour": "open", "vessel": "research"}'
    manifest = {
        "cutoff": "2026-10-22T00:00:00Z",
        "captures": [
            {
                "url": "https://example.com/status",
                "captured_at": "2026-10-20T00:00:00Z",
                "hash": digest(body),
                "content_type": "application/json",
                "title": "Harbour status",
            }
        ],
    }
    return frozen.import_corpus(manifest, {"https://example.com/status": body})


def test_frozen_import_pins_capture_time_hash_and_local_fetch_search(replay):
    world, _, frozen = replay
    pinned = corpus(frozen)
    run_id = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    reply = frozen.fetch(
        run_id, "a_frozen", "https://example.com/status#fragment", pinned["corpus_id"], inline=True
    )
    assert reply["kind"] == "result" and reply["provenance"]["capture_mode"] == "frozen"
    assert reply["provenance"]["cutoff"] == "2026-10-22T00:00:00.000000+00:00"
    assert json.loads(reply["inline"])["harbour"] == "open"
    assert reply["provenance"]["origin"] == "example.com"
    assert reply["provenance"]["fetch_ts"] == "2026-10-20T00:00:00.000000+00:00"
    assert reply["provenance"]["injection_score"] is None
    indexed = json.loads((world.store.run_dir(run_id) / "archive-index.json").read_text())
    assert (
        indexed[canonical_url("https://example.com/status")]["reply"]["provenance"]
        == reply["provenance"]
    )
    assert (
        frozen.search(pinned["corpus_id"], "research")["items"][0]["url"]
        == "https://example.com/status"
    )
    assert (
        frozen.fetch(run_id, "a_missing", "https://missing.example/", pinned["corpus_id"])["type"]
        == "replay_miss"
    )


def test_frozen_rejects_future_capture_and_wrong_hash(replay):
    _, _, frozen = replay
    manifest = {
        "cutoff": "2026-10-01T00:00:00Z",
        "captures": [
            {
                "url": "https://example.com/status",
                "captured_at": "2026-10-02T00:00:00Z",
                "hash": digest(b"ok"),
            }
        ],
    }
    with pytest.raises(ValueError, match="postdates"):
        frozen.import_corpus(manifest, {"https://example.com/status": b"ok"})
    manifest["cutoff"] = "2026-10-03T00:00:00Z"
    with pytest.raises(ValueError, match="hash mismatch"):
        frozen.import_corpus(manifest, {"https://example.com/status": b"changed"})


def test_frozen_rereads_and_verifies_bytes_instead_of_trusting_index(replay):
    world, _, frozen = replay
    pinned = corpus(frozen)
    run_id = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    capture = pinned["manifest"]["captures"][0]
    path = frozen.root / pinned["corpus_id"] / (capture["hash"].split(":")[1] + ".bin")
    path.chmod(0o600)
    path.write_text("tampered")
    reply = frozen.fetch(run_id, "a_tampered", capture["url"], pinned["corpus_id"])
    assert reply["rule_id"] == "frozen_integrity"


async def test_frozen_mode_integrates_world_fetch_search_with_zero_network_fallback(tmp_path):
    from worldd.app import create_app
    from worldd.gateway import Gateway
    from worldd.models import RunConfig

    calls = []

    def forbidden_network(request):
        calls.append(str(request.url))
        raise AssertionError("frozen mode attempted a live request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_network)) as client:
        gateway = Gateway(tmp_path, client=client)
        app = create_app(tmp_path, gateway=gateway)
        world = app.state.world
        try:
            pinned = corpus(world.services["frozen"])
            run_id = world.create_run(RunConfig(mode="frozen", corpus_id=pinned["corpus_id"]))[
                "run_id"
            ]
            result = await world.act(
                run_id, "fetch", {"url": "https://example.com/status", "inline": True}
            )
            assert result["provenance"]["capture_mode"] == "frozen"
            assert json.loads(result["inline"])["harbour"] == "open"
            results = await world.act(run_id, "search", {"query": "research"})
            assert results["provider_id"] == f"local@{pinned['corpus_id']}"
            miss = await world.act(run_id, "fetch", {"url": "https://not-in-corpus.example/nope"})
            assert miss["type"] == "replay_miss"
            assert calls == []
        finally:
            await world.aclose()


async def test_wayback_bootstrap_pins_real_cdx_selection_and_verifies_archive_checksum(replay):
    world, _, frozen = replay
    source = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    body = b"<html><p>A captured harbour timetable.</p></html>"
    checksum = base64.b32encode(hashlib.sha1(body).digest()).decode()
    calls = []

    async def read(run_id, url):
        calls.append(url)
        data = (
            json.dumps(
                [
                    ["timestamp", "original", "statuscode", "mimetype", "digest"],
                    [
                        "20261020000000",
                        "https://example.com/timetable",
                        "200",
                        "text/html",
                        checksum,
                    ],
                ]
            ).encode()
            if "/cdx/" in url
            else body
        )
        return {"kind": "result", "body": data, "hash": digest(data), "final_url": url}

    world.fetch_kernel = read
    imported = await frozen.from_wayback(
        source, ["https://example.com/timetable"], "2026-10-22T00:00:00Z"
    )
    capture = imported["manifest"]["captures"][0]
    assert len(calls) == 2 and "to=20261022000000" in calls[0]
    assert capture["captured_at"] == "2026-10-20T00:00:00.000000+00:00"
    assert capture["archive_provenance"]["archive_payload_digest_verified"]


async def test_wayback_checksum_failure_blocks_import_without_trust_bypass(replay):
    world, index, frozen = replay
    source = world.store.create_run({"budget": {"usd": 1}})["run_id"]

    async def read(run_id, url):
        data = (
            json.dumps(
                [
                    ["timestamp", "original", "statuscode", "mimetype", "digest"],
                    [
                        "20261020000000",
                        "https://example.com/timetable",
                        "200",
                        "text/html",
                        "A" * 32,
                    ],
                ]
            ).encode()
            if "/cdx/" in url
            else b"Changed archive bytes"
        )
        return {"kind": "result", "body": data, "hash": digest(data), "final_url": url}

    world.fetch_kernel = read
    result = await frozen.from_wayback(
        source, ["https://example.com/timetable"], "2026-10-22T00:00:00Z"
    )
    assert result["kind"] == "blocked" and result["rule_id"] == "wayback_integrity"
    assert index.db.execute("SELECT count(*) FROM frozen_corpora").fetchone()[0] == 0


def test_frozen_provenance_retains_measured_scores_and_omits_cookie_headers(replay):
    world, _, frozen = replay
    body = b"recorded source"
    pinned = frozen.import_corpus(
        {
            "cutoff": "2026-10-22T00:00:00Z",
            "captures": [
                {
                    "url": "https://example.com/capture",
                    "captured_at": "2026-10-20T00:00:00Z",
                    "hash": digest(body),
                    "content_type": "text/plain",
                    "provenance": {
                        "injection_score": 0.7,
                        "classifier_id": "source-classifier@1",
                        "classifier_status": "uncalibrated",
                    },
                    "headers_subset": {
                        "ETag": "recorded-tag",
                        "Set-Cookie": "fixture-private-session",
                    },
                }
            ],
        },
        {"https://example.com/capture": body},
    )
    run_id = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    reply = frozen.fetch(run_id, "a_provenance", "https://example.com/capture", pinned["corpus_id"])
    assert reply["provenance"]["injection_score"] == 0.7
    assert reply["provenance"]["classifier_id"] == "source-classifier@1"
    assert reply["headers_subset"] == {"etag": "recorded-tag"}
    assert "fixture-private-session" not in str(reply) + str(pinned)
