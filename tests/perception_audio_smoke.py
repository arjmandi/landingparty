"""Explicit offline image check: actual MP3 decoding and unspent vendor quotes."""

import asyncio
import io
import json
import subprocess
import tempfile
import wave
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from types import SimpleNamespace

from worldd.perception import PerceptionService, audio_seconds
from worldd.storage import Store


def encode():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 32000)
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
        input=buffer.getvalue(),
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout


async def main():
    encoded = await asyncio.to_thread(encode)
    duration = await asyncio.to_thread(audio_seconds, encoded)
    with tempfile.TemporaryDirectory(prefix="perception-audio-") as directory:
        store = Store(Path(directory))
        run_id = store.create_run({"budget": {"usd": 1, "wall_s": 60}})["run_id"]
        root = store.run_dir(run_id) / "world/fetched"
        root.mkdir(parents=True)
        (root / "audio.mp3").write_bytes(encoded)
        service = PerceptionService(SimpleNamespace(store=store))
        try:
            result = await service.handle(
                run_id, "a_quote", "transcribe", {"handle": "/world/fetched/audio.mp3"}
            )
            quotes = result["quotes"]
            checks = {
                "actual_nonseekable_mp3_decoded": 2 <= duration < 2.2,
                "three_declared_vendor_quotes": {row["provider"] for row in quotes}
                == {"openai", "deepgram", "assemblyai"},
                "quotes_bind_actual_decoded_duration": all(
                    abs(float(row["units"]) * 60 - duration) < 1e-9 for row in quotes
                ),
                "prices_equal_frozen_units_times_rate": all(
                    Decimal(row["usd"])
                    == (Decimal(row["units"]) * Decimal(row["unit_price_usd"])).quantize(
                        Decimal("0.000000001"), rounding=ROUND_CEILING
                    )
                    for row in quotes
                ),
                "quote_never_submits_or_spends": store.total_spent(run_id)
                == store.reserved(run_id)
                == 0,
            }
            print(
                json.dumps(
                    {
                        "kind": "perception_audio_software_verification",
                        "scientific_gate_evidence": False,
                        "external_provider_calls": 0,
                        "decoded_mp3_duration_s": duration,
                        "checks": checks,
                        "passed": all(checks.values()),
                    }
                )
            )
            if not all(checks.values()):
                raise SystemExit(1)
        finally:
            await service.aclose()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
