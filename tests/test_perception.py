import asyncio
import io
import shutil
import subprocess
import wave
from types import SimpleNamespace

import pymupdf
import pytest

from worldd.perception import PerceptionProvider, PerceptionService, audio_seconds
from worldd.storage import Store


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.run_id = store.create_run(
        {"budget": {"usd": 20, "wall_s": 7200}, "research_mode": "development"}
    )["run_id"]
    (store.run_dir(world.run_id) / "world" / "fetched").mkdir(parents=True)
    yield world
    store.close()


def pdf(world):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((30, 30), "Actual PDF content.")
        raw = document.tobytes()
    path = world.store.run_dir(world.run_id) / "world" / "fetched" / "document.pdf"
    path.write_bytes(raw)
    return "/world/fetched/document.pdf"


def audio(world):
    output = io.BytesIO()
    with wave.open(output, "wb") as document:
        document.setnchannels(1)
        document.setsampwidth(2)
        document.setframerate(8000)
        document.writeframes(b"\0\0" * 8000)
    path = world.store.run_dir(world.run_id) / "world" / "fetched" / "audio.wav"
    path.write_bytes(output.getvalue())
    return "/world/fetched/audio.wav"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Requires production ffmpeg/ffprobe dependency",
)
async def test_nonseekable_mp3_uses_decoded_duration_for_three_unspent_quotes(world):
    wav = audio(world)
    raw = (world.store.run_dir(world.run_id) / wav.lstrip("/")).read_bytes()
    encoded_result = await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
        input=raw,
        capture_output=True,
        check=True,
        timeout=10,
    )
    encoded = encoded_result.stdout
    measured = audio_seconds(encoded)
    assert 1 <= measured < 1.2
    (world.store.run_dir(world.run_id) / "world/fetched/audio.mp3").write_bytes(encoded)
    service = PerceptionService(world)
    try:
        quoted = await service.handle(
            world.run_id, "a_mp3_quote", "transcribe", {"handle": "/world/fetched/audio.mp3"}
        )
        assert {row["provider"] for row in quoted["quotes"]} == {"openai", "deepgram", "assemblyai"}
        assert all(abs(float(row["units"]) * 60 - measured) < 1e-9 for row in quoted["quotes"])
        assert world.store.total_spent(world.run_id) == world.store.reserved(world.run_id) == 0
        with pytest.raises(ValueError, match="truncated"):
            audio_seconds(raw[:-2])
        with pytest.raises(ValueError, match="Unable to determine"):
            audio_seconds(b"#EXTM3U\nhttp://127.0.0.1/never-open-this-source\n")
    finally:
        await service.aclose()


async def test_pdf_quote_is_unspent_then_explicitly_accepted_and_cached(world):
    service = PerceptionService(world, providers={})
    handle = pdf(world)
    quote = await service.handle(world.run_id, "a_quote", "parse_document", {"handle": handle})
    assert quote["acceptance_required"] and world.store.total_spent(world.run_id) == 0
    quote_id = next(q["quote_id"] for q in quote["quotes"] if q["provider"] == "local-pdf")
    request = {"handle": handle, "provider": "local-pdf", "quote_id": quote_id}
    parsed = await service.handle(world.run_id, "a_parse", "parse_document", request)
    assert "Actual PDF content" in parsed["pages"][0]["text"]
    assert parsed["retention_until"]
    assert await service.handle(world.run_id, "a_again", "parse_document", request) == parsed
    assert world.store.reconcile(world.run_id)["valid"]
    await service.aclose()


async def test_quote_cannot_be_reused_for_different_source_or_options(world):
    service = PerceptionService(world)
    handle = pdf(world)
    quote = await service.handle(world.run_id, "a_quote", "parse_document", {"handle": handle})
    quote_id = quote["quotes"][0]["quote_id"]
    changed = await service.handle(
        world.run_id,
        "a_parse",
        "parse_document",
        {"handle": handle, "pages": [1], "quote_id": quote_id},
    )
    assert changed["rule_id"] == "perception_quote"
    with pytest.raises(ValueError):
        await service.handle(
            world.run_id,
            "a_escape",
            "parse_document",
            {"handle": "/world/fetched/../../../private.pdf"},
        )
    await service.aclose()


async def test_transcription_uses_actual_duration_and_exact_paid_quote(world):
    calls = []

    async def vendor(raw, options):
        calls.append((raw, options))
        return {"text": "A spoken sentence.", "segments": [{"start": 0, "end": 1}]}

    provider = PerceptionProvider(
        "test-vendor", operation="transcribe", unit_price="0.006", invoke=vendor, test_only=True
    )
    service = PerceptionService(world, providers={"test-vendor": provider})
    handle = audio(world)
    options = {"handle": handle, "provider": "test-vendor", "timestamps": True}
    quote = await service.handle(world.run_id, "a_quote", "transcribe", options)
    assert quote["payment_required"] and len(calls) == 0
    result = await service.handle(
        world.run_id,
        "a_call",
        "transcribe",
        {**options, "quote_id": quote["quotes"][0]["quote_id"]},
    )
    assert result["text"] == "A spoken sentence."
    assert world.store.spend(world.run_id)["perception"] == float(result["usd"])
    assert world.store.reserved(world.run_id) == 0
    assert calls[0][1] == {"timestamps": True}
    await service.aclose()


async def test_unknown_billing_keeps_hold_and_does_not_retry(world):
    count = 0

    async def vendor(raw, options):
        nonlocal count
        count += 1
        raise TimeoutError("unknown provider usage")

    provider = PerceptionProvider(
        "test-vendor", operation="transcribe", unit_price="0.006", invoke=vendor, test_only=True
    )
    service = PerceptionService(world, providers={"test-vendor": provider})
    params = {"handle": audio(world), "provider": "test-vendor"}
    quote = await service.handle(world.run_id, "a_quote", "transcribe", params)
    params["quote_id"] = quote["quotes"][0]["quote_id"]
    with pytest.raises(TimeoutError):
        await service.handle(world.run_id, "a_call", "transcribe", params)
    assert world.store.reserved(world.run_id) > 0
    assert world.store.get_run(world.run_id)["tainted"]
    assert (await service.handle(world.run_id, "a_retry", "transcribe", params))[
        "rule_id"
    ] == "perception_quote_state"
    assert count == 1
    await service.aclose()


async def test_local_page_selection_rejects_out_of_document_pages(world):
    service = PerceptionService(world)
    handle = pdf(world)
    with pytest.raises(ValueError):
        await service.handle(
            world.run_id, "a_quote", "parse_document", {"handle": handle, "pages": [2]}
        )
    await service.aclose()


async def test_document_parsing_runs_off_event_loop_and_prunes_owned_output(world, monkeypatch):
    import threading
    from datetime import UTC, datetime, timedelta

    import worldd.perception as module

    original_measure, original_extract = module.pdf_pages, module._extract_pdf
    event_thread, worker_threads = threading.get_ident(), []

    def measure(raw):
        worker_threads.append(threading.get_ident())
        return original_measure(raw)

    def extract(raw, options):
        worker_threads.append(threading.get_ident())
        return original_extract(raw, options)

    monkeypatch.setattr(module, "pdf_pages", measure)
    monkeypatch.setattr(module, "_extract_pdf", extract)
    service = PerceptionService(world)
    handle = pdf(world)
    quotes = await service.handle(world.run_id, "a_quote", "parse_document", {"handle": handle})
    parsed = await service.handle(
        world.run_id,
        "a_parse",
        "parse_document",
        {"handle": handle, "quote_id": quotes["quotes"][0]["quote_id"]},
    )
    assert worker_threads and all(worker != event_thread for worker in worker_threads)
    output = world.store.run_dir(world.run_id) / parsed["handle"].lstrip("/")
    assert output.is_file()
    assert service.prune_expired(datetime.now(UTC) + timedelta(days=91))["pruned"] == 1
    assert not output.exists()
    assert (world.store.run_dir(world.run_id) / handle.lstrip("/")).is_file()
    assert (
        "Actual PDF content"
        not in service.db.execute("SELECT result FROM perception_quotes").fetchone()[0]
    )
    await service.aclose()


async def test_handle_only_parsing_preserves_observed_platform_deadline(world):
    from datetime import UTC, datetime, timedelta

    service = PerceptionService(world)
    handle = pdf(world)
    world.store.append_record(
        world.run_id,
        "outcome",
        action_id="a_fetch",
        reply={
            "kind": "result",
            "handle": handle,
            "url": "https://youtu.be/example",
            "status": 200,
        },
    )
    quotes = await service.handle(world.run_id, "a_quote", "parse_document", {"handle": handle})
    result = await service.handle(
        world.run_id,
        "a_parse",
        "parse_document",
        {"handle": handle, "quote_id": quotes["quotes"][0]["quote_id"]},
    )
    deadline = datetime.fromisoformat(result["retention_until"])
    assert (
        datetime.now(UTC) + timedelta(days=29) < deadline < datetime.now(UTC) + timedelta(days=31)
    )
    await service.aclose()
