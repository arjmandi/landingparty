import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from worldd.retention import RetentionService
from worldd.storage import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "data")
    clock = SimpleNamespace(now=datetime.now(UTC))
    service = RetentionService(SimpleNamespace(store=store), clock=lambda: clock.now)
    run_id = store.create_run({"budget": {"usd": 1}})["run_id"]
    fetched = store.run_dir(run_id) / "world" / "fetched"
    fetched.mkdir(parents=True)
    yield store, service, clock, run_id, fetched
    store.close()


def test_expiry_preserves_hash_and_text_without_changing_sealed_record(setup):
    store, retention, clock, run_id, fetched = setup
    raw = fetched / "a_1.html"
    body = b"<html><h1>Observed claim</h1><script>ignore</script></html>"
    raw.write_bytes(body)
    before = store.records(run_id)
    retention.register(run_id, raw, content_type="text/html")
    clock.now += timedelta(days=89)
    assert retention.sweep()["pruned"] == []
    assert raw.is_file()
    clock.now += timedelta(days=1)
    result = retention.sweep()
    assert len(result["pruned"]) == 1
    assert not raw.exists()
    entry = retention.resources(run_id)[0]
    assert entry["hash"] == "sha256:" + hashlib.sha256(body).hexdigest()
    text = (store.run_dir(run_id) / entry["text_path"]).read_text()
    assert text == "Observed claim"
    assert store.records(run_id) == before
    assert store.verify_record(run_id)["valid"]
    assert retention.sweep()["pruned"] == []


def test_platform_expiry_shortens_and_reregistration_never_extends(setup):
    _, retention, clock, run_id, fetched = setup
    raw = fetched / "a_1.txt"
    raw.write_text("platform-derived text")
    deadline = clock.now + timedelta(days=3)
    entry = retention.register(run_id, raw, retention_until=deadline.isoformat())
    clock.now += timedelta(days=1)
    again = retention.register(run_id, raw)
    assert again["retention_until"] == entry["retention_until"]
    clock.now += timedelta(days=2)
    assert len(retention.sweep()["pruned"]) == 1


def test_private_provider_capture_is_owned_and_expires_with_hash(setup):
    store, retention, clock, run_id, _ = setup
    path = store.run_dir(run_id) / "private-responses" / "provider.json"
    path.parent.mkdir()
    path.write_text('{"private_provider_output":"archived evidence"}')
    assert len(retention.register_run(run_id)) == 1
    clock.now += timedelta(days=90)
    assert len(retention.sweep()["pruned"]) == 1
    assert not path.exists()
    assert retention.resources(run_id)[0]["hash"]


def test_late_discovery_never_restarts_ninety_day_capture_clock(setup):
    store, retention, clock, run_id, fetched = setup
    raw = fetched / "late.txt"
    raw.write_text("Old capture discovered after restart")
    created_at = store.get_run(run_id)["created_at"]
    clock.now += timedelta(days=91)
    resource = retention.register(run_id, raw)
    assert datetime.fromisoformat(resource["retention_until"]) <= datetime.fromisoformat(
        created_at
    ) + timedelta(days=90)
    assert len(retention.sweep()["pruned"]) == 1


def test_workspace_record_beliefs_and_outside_paths_cannot_be_registered(setup, tmp_path):
    store, retention, _, run_id, _ = setup
    root = store.run_dir(run_id)
    for relative in ("work/out/important.txt", "beliefs/ledger.jsonl", "vault/key"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("must remain")
        with pytest.raises(ValueError):
            retention.register(run_id, target)
        assert target.read_text() == "must remain"
    with pytest.raises(ValueError):
        retention.register(run_id, root / "record.jsonl")
    outside = tmp_path / "external.txt"
    outside.write_text("outside")
    with pytest.raises(ValueError):
        retention.register(run_id, outside)
    with pytest.raises(ValueError):
        retention.register(run_id, root)


def test_symlinked_capture_never_deletes_target(setup, tmp_path):
    _, retention, _, run_id, fetched = setup
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    raw = fetched / "a_link.txt"
    raw.symlink_to(outside)
    with pytest.raises(ValueError):
        retention.register(run_id, raw)
    assert outside.read_text() == "private"


def test_missing_capture_is_distinct_from_successful_expiry(setup):
    _, retention, clock, run_id, fetched = setup
    raw = fetched / "a_1.txt"
    raw.write_text("later missing")
    retention.register(run_id, raw)
    raw.unlink()
    clock.now += timedelta(days=90)
    outcome = retention.sweep()
    assert len(outcome["missing"]) == 1
    assert outcome["pruned"] == []
    assert retention.resources(run_id)[0]["state"] == "missing"


def test_expiry_crash_after_unlink_keeps_committed_evidence_manifest(setup, monkeypatch):
    store, retention, clock, run_id, fetched = setup
    raw = fetched / "a_1.txt"
    raw.write_text("retained evidence")
    retention.register(run_id, raw)
    clock.now += timedelta(days=90)
    original = Path.unlink

    def interrupt_after_delete(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path == raw:
            raise OSError("simulated crash after filesystem mutation")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt_after_delete)
        assert len(retention.sweep()["failed"]) == 1
    pending = retention.resources(run_id)[0]
    assert pending["state"] == "expiring"
    assert pending["hash"]
    assert (store.run_dir(run_id) / pending["text_path"]).read_text() == "retained evidence"
    assert len(retention.sweep()["pruned"]) == 1
    assert retention.resources(run_id)[0]["state"] == "expired"


def test_warc_retains_response_text_and_content_hash(setup):
    store, retention, clock, run_id, _ = setup
    path = store.run_dir(run_id) / "archive.warc.gz"
    body = b"<h1>Archived source</h1>"
    with path.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True)
        record = writer.create_warc_record(
            "https://example.org/source",
            "response",
            payload=io.BytesIO(body),
            http_headers=StatusAndHeaders(
                "200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
            ),
        )
        writer.write_record(record)
    retention.register(run_id, path)
    clock.now += timedelta(days=90)
    assert len(retention.sweep()["pruned"]) == 1
    entry = retention.resources(run_id)[0]
    source = json.loads((store.run_dir(run_id) / entry["text_path"]).read_text())
    assert source["url"] == "https://example.org/source"
    assert source["text"] == "Archived source"
    assert source["hash"] == "sha256:" + hashlib.sha256(body).hexdigest()


def test_malformed_har_is_retained_when_text_preservation_cannot_complete(setup):
    store, retention, clock, run_id, _ = setup
    path = store.run_dir(run_id) / "archive.har"
    path.write_text("{invalid json")
    retention.register(run_id, path)
    clock.now += timedelta(days=90)
    assert len(retention.sweep()["failed"]) == 1
    assert path.exists()
    assert retention.resources(run_id)[0]["state"] == "raw"


def test_symlinked_retained_directory_is_rejected_before_any_write(setup, tmp_path):
    store, retention, clock, run_id, fetched = setup
    raw = fetched / "a_1.txt"
    raw.write_text("never write outside")
    retention.register(run_id, raw)
    external = tmp_path / "external"
    external.mkdir()
    (store.run_dir(run_id) / "retained").symlink_to(external, target_is_directory=True)
    clock.now += timedelta(days=90)
    assert len(retention.sweep()["failed"]) == 1
    assert list(external.iterdir()) == []
    assert raw.exists()
