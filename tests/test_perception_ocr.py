import base64
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pymupdf
import pytest

from worldd.gateway import Gateway
from worldd.perception import MISTRAL_OCR_MODEL, PerceptionService, default_pricing_identity
from worldd.provider_boundary import submit_provider
from worldd.storage import Store


def pdf(store, run_id):
    with pymupdf.open() as document:
        for text in ("First PDF page", "Second PDF page"):
            document.new_page().insert_text((40, 40), text)
        raw = document.tobytes()
    destination = store.run_dir(run_id) / "world" / "fetched" / "document.pdf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return raw


@pytest.mark.parametrize("mutate_body,bad_usage", [(False, False), (True, False), (False, True)])
async def test_priced_ocr_has_exact_private_request_quote_and_usage(
    tmp_path, monkeypatch, mutate_body, bad_usage
):
    monkeypatch.setenv("WORLD_MISTRAL_API_KEY", "fixture-ocr-provider-secret")
    calls = []

    async def dns(host, port):
        assert host == "api.mistral.ai"
        return ["93.184.216.34"]

    def vendor(request):
        calls.append(request)
        assert (
            request.url.path == "/v1/ocr"
            and request.headers["authorization"] == "Bearer fixture-ocr-provider-secret"
        )
        body = json.loads(request.content)
        assert body["model"] == MISTRAL_OCR_MODEL and body["pages"] == [1]
        assert base64.b64decode(body["document"]["document_url"].split(",", 1)[1]) == raw
        return httpx.Response(
            200,
            json={
                "model": MISTRAL_OCR_MODEL,
                "pages": [{"index": 1, "markdown": "# Actual fixture OCR"}],
                "usage_info": {"pages_processed": 2 if bad_usage else 1},
                "private": "unexposed-vendor-field",
            },
        )

    store = Store(tmp_path)
    run_id = store.create_run(
        {
            "research_mode": "research",
            "mode": "live",
            "budget": {"usd": 1, "wall_s": 300},
            "perception_pricing": default_pricing_identity(),
        }
    )["run_id"]
    raw = pdf(store, run_id)
    async with httpx.AsyncClient(transport=httpx.MockTransport(vendor)) as client:
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client, resolver=dns)
        world = SimpleNamespace(store=store, gateway=gateway, services={}, semaphores={})

        async def send(run, action, provider, request, **kwargs):
            if mutate_body:
                body = json.loads(request.content)
                body["pages"] = [0]
                request = httpx.Request(
                    request.method, request.url, json=body, headers=request.headers
                )
            return await submit_provider(world, run, action, provider, request, **kwargs)

        world.submit_provider = send
        perception = PerceptionService(world)
        world.services["perception"] = perception
        params = {"handle": "/world/fetched/document.pdf", "provider": "mistral-ocr", "pages": [2]}
        quoted = (await perception.handle(run_id, "a_quote", "parse_document", params))["quotes"][0]
        assert (
            quoted["usd"] == "0.002000000"
            and quoted["provider_identity"]["model"] == MISTRAL_OCR_MODEL
        )
        assert not calls and store.total_spent(run_id) == 0
        params["quote_id"] = quoted["quote_id"]
        try:
            if bad_usage:
                with pytest.raises(ValueError, match="usage/pages"):
                    await perception.handle(run_id, "a_ocr", "parse_document", params)
                assert store.reserved(run_id) == 0.002 and store.get_run(run_id)["tainted"]
            else:
                result = await perception.handle(run_id, "a_ocr", "parse_document", params)
                if mutate_body:
                    assert (
                        result["rule_id"] == "provider_quote_body"
                        and not calls
                        and store.reserved(run_id) == 0
                    )
                else:
                    assert result["pages"] == [{"page": 2, "text": "# Actual fixture OCR"}]
                    assert (
                        store.spend(run_id)["perception"] == 0.002 and store.reserved(run_id) == 0
                    )
                    assert "private" not in result and "unexposed-vendor-field" not in json.dumps(
                        result
                    )
                    child = next(
                        run
                        for run in store.list_runs()
                        if run["config"].get("realm") == "perception"
                    )
                    assert (
                        child["config"]["private_archive"]
                        and store.verify_record(child["run_id"])["valid"]
                    )
                    assert (
                        "fixture-ocr-provider-secret"
                        not in (store.run_dir(run_id) / "record.jsonl").read_text()
                    )
            assert len(calls) == (0 if mutate_body else 1)
        finally:
            await perception.aclose()
            await gateway.aclose()
    store.close()


async def test_unconfigured_three_vendor_quotes_and_frozen_price_drift(tmp_path, monkeypatch):
    import io
    import wave

    for key in (
        "WORLD_TRANSCRIBE_OPENAI_KEY",
        "WORLD_DEEPGRAM_API_KEY",
        "WORLD_ASSEMBLYAI_API_KEY",
        "WORLD_MISTRAL_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    store = Store(tmp_path)
    run_id = store.create_run(
        {"budget": {"usd": 1, "wall_s": 300}, "perception_pricing": default_pricing_identity()}
    )["run_id"]
    world = SimpleNamespace(store=store)
    service = PerceptionService(world)
    source = store.run_dir(run_id) / "world" / "fetched"
    source.mkdir(parents=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    (source / "audio.wav").write_bytes(buffer.getvalue())
    quotes = (
        await service.handle(
            run_id, "a_quote", "transcribe", {"handle": "/world/fetched/audio.wav"}
        )
    )["quotes"]
    assert {quote["provider"] for quote in quotes} == {"openai", "deepgram", "assemblyai"}
    assert all(not quote["available"] for quote in quotes)
    denied = await service.handle(
        run_id,
        "a_unconfigured",
        "transcribe",
        {
            "handle": "/world/fetched/audio.wav",
            "provider": quotes[0]["provider"],
            "quote_id": quotes[0]["quote_id"],
        },
    )
    assert denied["rule_id"] == "perception_provider_unavailable" and store.reserved(run_id) == 0
    pdf(store, run_id)
    monkeypatch.setenv("WORLD_MISTRAL_API_KEY", "fixture-ocr-key")
    service.providers["mistral-ocr"].unit_price = Decimal("0.003")
    params = {"handle": "/world/fetched/document.pdf", "provider": "mistral-ocr"}
    quote = (await service.handle(run_id, "a_ocr_quote", "parse_document", params))["quotes"][0]
    denied = await service.handle(
        run_id, "a_drift", "parse_document", {**params, "quote_id": quote["quote_id"]}
    )
    assert denied["rule_id"] == "perception_price_drift" and store.reserved(run_id) == 0
    await service.aclose()
    store.close()
