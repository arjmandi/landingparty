"""Explicit, bound perception quotes with actual audio/PDF size inspection.

Returning a quote does not spend money. Passing its quote_id accepts exactly
that provider, source hash, options, units, and price. Vendor transports are
injectable; missing credentials are reported rather than replaced with data.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import subprocess
import threading
import wave
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .identity import VenueDB
from .util import blocked, canonical, digest, now

_PROVIDER_CONTEXT = ContextVar("landing_party_provider_context", default=None)
_PDF_LOCK = threading.Lock()
MISTRAL_OCR_MODEL = "mistral-ocr-2505"


def default_pricing_identity():
    """Pure declared price/model map, independent of credentials or databases."""
    providers = {
        "openai": ("transcribe", "WORLD_OPENAI_USD_PER_MINUTE", "0.006", "whisper-1"),
        "deepgram": ("transcribe", "WORLD_DEEPGRAM_USD_PER_MINUTE", "0.0043", "nova-3"),
        "assemblyai": (
            "transcribe",
            "WORLD_ASSEMBLYAI_USD_PER_MINUTE",
            "0.0035",
            "provider-default",
        ),
        "mistral-ocr": (
            "parse_document",
            "WORLD_MISTRAL_OCR_USD_PER_PAGE",
            "0.002",
            MISTRAL_OCR_MODEL,
        ),
        "local-pdf": ("parse_document", None, "0", "pymupdf"),
    }
    result = {}
    for name, (operation, binding, fallback, model) in sorted(providers.items()):
        price = Decimal(os.environ.get(binding, fallback) if binding else fallback)
        if not price.is_finite() or price < 0:
            raise ValueError("Declared perception prices must be finite and nonnegative")
        result[name] = {"operation": operation, "unit_price_usd": str(price), "model": model}
    return result


class ProviderBoundaryDenied(Exception):
    def __init__(self, reply):
        self.reply = reply
        super().__init__("The world boundary refused the perception provider request")


class PerceptionProvider:
    def __init__(
        self,
        name,
        *,
        operation,
        unit_price,
        invoke,
        test_only=False,
        credential_env=None,
        credential_resolver=None,
    ):
        self.name, self.operation = name, operation
        self.unit_price = Decimal(str(unit_price))
        if not self.unit_price.is_finite() or self.unit_price < 0:
            raise ValueError("Provider unit price must be finite and nonnegative")
        self.invoke, self.test_only = invoke, test_only
        self.credential_env = credential_env
        self.credential_resolver = credential_resolver or os.environ.get

    @property
    def available(self):
        return not self.credential_env or bool(self.credential_resolver(self.credential_env))


def audio_seconds(raw):
    try:
        with wave.open(io.BytesIO(raw)) as audio:
            count, rate = audio.getnframes(), audio.getframerate()
            frames = audio.readframes(count)
            if count <= 0 or len(frames) != count * audio.getnchannels() * audio.getsampwidth():
                raise ValueError("Audio frame data is empty or truncated")
            return count / rate
    except (wave.Error, EOFError):
        # The subprocess reads only its input pipe, including nested-format references.
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate:frame=nb_samples",
                "-of",
                "json",
                "pipe:0",
            ],
            input=raw,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ValueError("Unable to determine audio duration from this resource")
        # Nonseekable MP3 and other streams omit format.duration. Count decoded
        # samples instead of guessing from a header or opening a source URL.
        measured = json.loads(result.stdout)
        streams = measured.get("streams", [])
        rate = int(streams[0]["sample_rate"]) if len(streams) == 1 else 0
        samples = sum(int(frame["nb_samples"]) for frame in measured.get("frames", []))
        duration = samples / rate if rate > 0 else 0
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Audio duration is missing or invalid")
        return duration


def pdf_pages(raw):
    import pymupdf

    with _PDF_LOCK, pymupdf.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("Encrypted document needs authentication")
        return len(document)


async def local_pdf(raw, options):
    return await asyncio.to_thread(_extract_pdf, raw, options)


def _extract_pdf(raw, options):
    import pymupdf

    with _PDF_LOCK, pymupdf.open(stream=raw, filetype="pdf") as document:
        selected = options.get("pages") or list(range(1, len(document) + 1))
        return {
            "pages": [{"page": page, "text": document[page - 1].get_text()} for page in selected],
            "extractor_id": "pymupdf@" + str(pymupdf.VersionBind),
        }


class PerceptionService(VenueDB):
    def _credential(self, name):
        resolver = getattr(self.world, "credential", os.environ.get)
        return resolver(name)

    def __init__(self, world, *, providers=None, client=None):
        super().__init__(world, "perception")
        self.client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        self.owns_client = client is None
        self.providers = dict(providers or {})
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS perception_quotes (
                quote_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, reservation_id TEXT);
        """)
        self.providers.setdefault(
            "local-pdf",
            PerceptionProvider(
                "local-pdf", operation="parse_document", unit_price="0", invoke=local_pdf
            ),
        )
        self._register_vendor("openai", "WORLD_TRANSCRIBE_OPENAI_KEY", "0.006")
        self._register_vendor("deepgram", "WORLD_DEEPGRAM_API_KEY", "0.0043")
        self._register_vendor("assemblyai", "WORLD_ASSEMBLYAI_API_KEY", "0.0035")
        self.providers.setdefault(
            "mistral-ocr",
            PerceptionProvider(
                "mistral-ocr",
                operation="parse_document",
                unit_price=os.environ.get("WORLD_MISTRAL_OCR_USD_PER_PAGE", "0.002"),
                invoke=self._mistral_ocr,
                credential_env="WORLD_MISTRAL_API_KEY",
                credential_resolver=self._credential,
            ),
        )

    @property
    def provider_rates(self):
        return {
            name: {
                "operation": p.operation,
                "unit_price_usd": str(p.unit_price),
                "available": p.available,
            }
            for name, p in sorted(self.providers.items())
        }

    def pricing_identity(self):
        defaults = default_pricing_identity()
        return {
            name: {
                "operation": provider.operation,
                "unit_price_usd": str(provider.unit_price),
                "model": defaults.get(name, {}).get("model", "registered-callback"),
            }
            for name, provider in sorted(self.providers.items())
        }

    def _register_vendor(self, vendor, env_key, rate):
        if vendor not in self.providers:

            async def invoke(raw, options):
                key = self._credential(env_key)
                if not key:
                    raise ValueError("The configured vendor credential is no longer available")
                return await self._transcribe(vendor, key, raw, options)

            self.providers[vendor] = PerceptionProvider(
                vendor,
                operation="transcribe",
                unit_price=os.environ.get(f"WORLD_{vendor.upper()}_USD_PER_MINUTE", rate),
                invoke=invoke,
                credential_env=env_key,
                credential_resolver=self._credential,
            )

    async def _mistral_ocr(self, raw, options):
        payload = {
            "model": MISTRAL_OCR_MODEL,
            "document": {
                "type": "document_url",
                "document_url": "data:application/pdf;base64," + base64.b64encode(raw).decode(),
            },
            "include_image_base64": False,
        }
        if options.get("pages"):
            payload["pages"] = [page - 1 for page in options["pages"]]
        response = await self._provider_request(
            "POST",
            "https://api.mistral.ai/v1/ocr",
            json=payload,
            headers={"Authorization": "Bearer " + self._credential("WORLD_MISTRAL_API_KEY")},
        )
        response.raise_for_status()
        result = response.json()
        expected = options.get("pages") or list(
            range(1, await asyncio.to_thread(pdf_pages, raw) + 1)
        )
        pages = result.get("pages")
        processed = result.get("usage_info", {}).get("pages_processed")
        if (
            isinstance(processed, bool)
            or not isinstance(processed, int)
            or processed != len(expected)
            or not isinstance(pages, list)
            or len(pages) != len(expected)
            or {page.get("index") for page in pages if isinstance(page, dict)}
            != {page - 1 for page in expected}
            or any(not isinstance(page.get("markdown"), str) for page in pages)
            or any(
                isinstance(page.get("index"), bool) or not isinstance(page.get("index"), int)
                for page in pages
            )
        ):
            raise ValueError("OCR usage/pages do not match the accepted page quote")
        if result.get("model") != MISTRAL_OCR_MODEL:
            raise ValueError("OCR provider changed the registered model snapshot")
        return {
            "pages": [
                {"page": page["index"] + 1, "text": page["markdown"]}
                for page in sorted(pages, key=lambda page: page["index"])
            ],
            "extractor_id": "mistral-ocr@" + MISTRAL_OCR_MODEL,
            "usage": {"pages_processed": processed},
        }

    async def _transcribe(self, vendor, key, raw, options):
        if vendor == "openai":
            # Timestamp/diarization-specific models must have an explicit registered price.
            if options.get("diarize"):
                raise ValueError("This OpenAI adapter's pinned whisper-1 model does not diarize")
            data = {"model": "whisper-1", "response_format": "verbose_json"}
            if options.get("language"):
                data["language"] = options["language"]
            response = await self._provider_request(
                "POST",
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + key},
                data=data,
                files={"file": ("resource.wav", raw, "application/octet-stream")},
            )
            response.raise_for_status()
            payload = response.json()
            return {
                "text": payload["text"],
                "segments": payload.get("segments", []) if options.get("timestamps") else [],
            }
        if vendor == "deepgram":
            params = {
                "model": "nova-3",
                "diarize": str(bool(options.get("diarize"))).lower(),
                "utterances": str(bool(options.get("timestamps"))).lower(),
            }
            if options.get("language"):
                params["language"] = options["language"]
            response = await self._provider_request(
                "POST",
                "https://api.deepgram.com/v1/listen",
                params=params,
                content=raw,
                headers={
                    "Authorization": "Token " + key,
                    "Content-Type": "application/octet-stream",
                },
            )
            response.raise_for_status()
            payload = response.json()["results"]
            alternative = payload["channels"][0]["alternatives"][0]
            return {
                "text": alternative["transcript"],
                "words": alternative.get("words", []) if options.get("timestamps") else [],
                "utterances": payload.get("utterances", []) if options.get("diarize") else [],
            }
        headers = {"Authorization": key}
        uploaded = await self._provider_request(
            "POST", "https://api.assemblyai.com/v2/upload", content=raw, headers=headers
        )
        uploaded.raise_for_status()
        payload = {
            "audio_url": uploaded.json()["upload_url"],
            "speaker_labels": bool(options.get("diarize")),
        }
        if options.get("language"):
            payload["language_code"] = options["language"]
        created = await self._provider_request(
            "POST", "https://api.assemblyai.com/v2/transcript", json=payload, headers=headers
        )
        created.raise_for_status()
        transcript_id = created.json()["id"]
        for _ in range(120):
            response = await self._provider_request(
                "GET",
                "https://api.assemblyai.com/v2/transcript/" + str(transcript_id),
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            if data["status"] == "completed":
                return {
                    "text": data["text"],
                    "words": data.get("words", []) if options.get("timestamps") else [],
                    "utterances": data.get("utterances", []) if options.get("diarize") else [],
                }
            if data["status"] == "error":
                raise ValueError("The transcription vendor reported a processing failure")
            await asyncio.sleep(2)
        raise TimeoutError("The transcription job remains pending")

    async def _provider_request(self, method, url, **kwargs):
        context = _PROVIDER_CONTEXT.get()
        sender = getattr(self.world, "submit_provider", None)
        if context is None or sender is None:
            raise ProviderBoundaryDenied(
                blocked(
                    "policy_deny",
                    "Vendor calls require the recorded provider boundary",
                    "provider_boundary",
                )
            )
        run_id, action_id, provider, reservation_id = context
        request = self.client.build_request(method, url, **kwargs)
        await request.aread()
        response = await sender(run_id, action_id, provider, request, reservation_id=reservation_id)
        if isinstance(response, dict):
            raise ProviderBoundaryDenied(response)
        return response

    def _resource(self, run_id, handle):
        if not isinstance(handle, str) or not handle.startswith("/world/fetched/"):
            raise ValueError("Perception accepts this run's fetched handles only")
        root = (self.store.run_dir(run_id) / "world" / "fetched").resolve()
        path = self.store.run_dir(run_id) / handle.lstrip("/")
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved != path or not path.is_file():
            raise ValueError("Resource handle does not resolve within this run")
        return path, path.read_bytes()

    def _retention_until(self, run_id, handle, path, params):
        source_time = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        deadline = min(datetime.now(UTC), source_time) + timedelta(days=90)
        sources = [(params.get("url"), source_time)]
        for record in self.store.records(run_id):
            resource = record.get("reply", {}) if record["kind"] == "outcome" else record
            if resource.get("handle") != handle:
                continue
            observed = datetime.fromisoformat(record["ts"])
            sources.extend((resource.get(key), observed) for key in ("url", "final_url"))
            if resource.get("retention_until"):
                deadline = min(deadline, datetime.fromisoformat(resource["retention_until"]))
        for url, observed in sources:
            host = (urlsplit(url).hostname or "").lower() if url else ""
            if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
                deadline = min(deadline, observed + timedelta(days=30))
        retention = getattr(self.world, "services", {}).get("retention")
        if retention:
            for resource in retention.resources(run_id):
                if resource["path"] == handle.lstrip("/"):
                    deadline = min(deadline, datetime.fromisoformat(resource["retention_until"]))
        return deadline.isoformat()

    def _quotes(self, run_id, operation, handle, raw, params, measured):
        options = {
            key: params.get(key)
            for key in ("diarize", "timestamps", "language", "pages")
            if params.get(key) is not None
        }
        if operation == "transcribe":
            duration = measured
            units, unit = Decimal(str(duration)) / 60, "minute"
        else:
            count = measured
            pages = options.get("pages")
            if pages and (
                len(set(pages)) != len(pages)
                or any(
                    isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > count
                    for page in pages
                )
            ):
                raise ValueError("Pages must be unique one-based page numbers in this document")
            units, unit = Decimal(len(pages) if pages else count), "page"
        quotes = []
        for name, provider in self.providers.items():
            if provider.operation != operation or (
                params.get("provider") and params["provider"] != name
            ):
                continue
            if operation == "transcribe" and name == "openai" and options.get("diarize"):
                continue
            binding = {
                "run_id": run_id,
                "operation": operation,
                "handle": handle,
                "source_hash": digest(raw),
                "provider": name,
                "provider_identity": self.pricing_identity()[name],
                "options": options,
                "units": str(units),
                "unit": unit,
                "unit_price_usd": str(provider.unit_price),
                "usd": str(
                    (units * provider.unit_price).quantize(
                        Decimal("0.000000001"), rounding=ROUND_CEILING
                    )
                ),
            }
            quote_id = digest(binding).replace("sha256:", "q_")
            existing = self.db.execute(
                "SELECT payload,state FROM perception_quotes WHERE quote_id=?", (quote_id,)
            ).fetchone()
            if existing:
                item = json.loads(existing["payload"])
                if (
                    item["expires_at"] <= now()
                    and existing["state"] == "quoted"
                    and not params.get("quote_id")
                ):
                    item["expires_at"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
                    self.db.execute(
                        "UPDATE perception_quotes SET payload=? WHERE quote_id=?",
                        (canonical(item), quote_id),
                    )
            else:
                item = {
                    **binding,
                    "quote_id": quote_id,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                }
                self.db.execute(
                    "INSERT INTO perception_quotes VALUES (?,?,?,'quoted',NULL,NULL)",
                    (quote_id, run_id, canonical(item)),
                )
            quotes.append({**item, "available": provider.available})
        return quotes

    async def handle(self, run_id, action_id, tool, params):
        if tool not in {"transcribe", "parse_document"}:
            raise ValueError("Unknown perception tool")
        handle = params.get("handle")
        if not handle and params.get("url"):
            fetch = getattr(self.world, "fetch_kernel", None)
            if fetch is None:
                return blocked(
                    "platform_block",
                    "URL perception requires the world fetch boundary",
                    "perception_fetch",
                )
            fetched = await fetch(run_id, params["url"])
            if fetched.get("kind") != "result":
                return blocked(
                    "platform_block",
                    "The platform did not provide the media resource",
                    "perception_platform",
                    source_block=fetched,
                )
            handle = fetched["handle"]
            if fetched.get("resource_path"):
                source = Path(fetched["resource_path"])
                archive_root = (self.store.data_dir / "runs").resolve()
                if (
                    source.resolve() != source
                    or not source.resolve().is_relative_to(archive_root)
                    or not source.is_file()
                ):
                    return blocked(
                        "policy_deny",
                        "Kernel fetch did not return a world-owned resource",
                        "perception_source",
                    )
                raw_source = source.read_bytes()
                parent = self.store.run_dir(run_id) / "world" / "fetched"
                parent.mkdir(parents=True, exist_ok=True)
                destination = parent / ("kernel_" + digest(raw_source).split(":", 1)[1] + ".bin")
                destination.write_bytes(raw_source)
                handle = "/world/fetched/" + destination.name
        source_path, raw = await asyncio.to_thread(self._resource, run_id, handle)
        retention_until = self._retention_until(run_id, handle, source_path, params)
        if retention_until <= now():
            return blocked(
                "policy_deny", "The source's declared retention expired", "retention_expired"
            )
        if params.get("url"):
            self.store.append_record(
                run_id,
                "perception_source",
                handle=handle,
                url=params["url"],
                source_hash=digest(raw),
                retention_until=retention_until,
            )
        measured = await asyncio.to_thread(
            audio_seconds if tool == "transcribe" else pdf_pages, raw
        )
        quotes = self._quotes(run_id, tool, handle, raw, params, measured)
        if not quotes:
            return blocked(
                "platform_block",
                "No matching priced perception provider is configured",
                "perception_provider",
            )
        if not params.get("quote_id"):
            return {
                "kind": "result",
                "quotes": quotes,
                "payment_required": any(Decimal(item["usd"]) > 0 for item in quotes),
                "acceptance_required": True,
            }
        selected = next((item for item in quotes if item["quote_id"] == params["quote_id"]), None)
        if selected is None:
            return blocked(
                "policy_deny",
                "Quote does not bind this resource, provider, and options",
                "perception_quote",
            )
        row = self.db.execute(
            "SELECT * FROM perception_quotes WHERE quote_id=?", (selected["quote_id"],)
        ).fetchone()
        if row["state"] == "complete":
            return json.loads(row["result"])
        if row["state"] != "quoted" or selected["expires_at"] <= now():
            return blocked(
                "policy_deny",
                "Quote expired or its prior outcome needs reconciliation",
                "perception_quote_state",
            )
        provider = self.providers[selected["provider"]]
        if not provider.available:
            return blocked(
                "policy_deny",
                "This quoted provider has no world-held credential configured",
                "perception_provider_unavailable",
            )
        config = self.store.get_run(run_id)["config"]
        frozen = config.get("perception_pricing")
        if frozen is not None and frozen.get(selected["provider"]) != selected["provider_identity"]:
            return blocked(
                "policy_deny",
                "Perception pricing/model differs from this run's frozen registration",
                "perception_price_drift",
            )
        if frozen is None and config.get("research_mode", "development") != "development":
            return blocked(
                "policy_deny",
                "Research perception requires frozen provider pricing",
                "perception_price_registration",
            )
        if (
            config.get("research_mode", "development") == "development"
            and selected["provider"] != "local-pdf"
            and not provider.test_only
        ):
            return blocked(
                "policy_deny",
                "Development runs do not spend on external perception vendors",
                "development_spend",
            )
        reservation = self.store.reserve(run_id, selected["usd"])
        if reservation is None:
            return blocked(
                "budget_exhausted",
                "The perception quote exceeds remaining budget",
                "perception_budget",
            )
        self.db.execute(
            "UPDATE perception_quotes SET state='processing',reservation_id=? WHERE quote_id=?",
            (reservation, selected["quote_id"]),
        )
        context = _PROVIDER_CONTEXT.set((run_id, action_id, selected["provider"], reservation))
        try:
            data = await provider.invoke(raw, selected["options"])
            if not isinstance(data, dict):
                raise TypeError("Perception provider must return a structured result")
        except ProviderBoundaryDenied as exc:
            if not exc.reply.get("egress_submitted", False):
                self.store.release(reservation)
                self.db.execute(
                    "UPDATE perception_quotes SET state='quoted',reservation_id=NULL WHERE quote_id=?",
                    (selected["quote_id"],),
                )
                return exc.reply
            self.db.execute(
                "UPDATE perception_quotes SET state='unknown' WHERE quote_id=?",
                (selected["quote_id"],),
            )
            self.store.update_run(run_id, tainted=True)
            self.store.append_record(
                run_id,
                "perception_usage_unknown",
                quote_id=selected["quote_id"],
                reserved_usd=selected["usd"],
            )
            return exc.reply
        except BaseException:
            self.db.execute(
                "UPDATE perception_quotes SET state='unknown' WHERE quote_id=?",
                (selected["quote_id"],),
            )
            self.store.update_run(run_id, tainted=True)
            self.store.append_record(
                run_id,
                "perception_usage_unknown",
                quote_id=selected["quote_id"],
                reserved_usd=selected["usd"],
            )
            raise
        finally:
            _PROVIDER_CONTEXT.reset(context)
        if not self.store.settle(reservation, "perception", selected["usd"]):
            raise ValueError("Perception quote settlement failed")
        output = self.store.run_dir(run_id) / "world" / "perception"
        output.mkdir(exist_ok=True)
        output_path = output / f"{action_id}.json"
        result = {
            **data,
            "kind": "result",
            "provider": selected["provider"],
            "quote_id": selected["quote_id"],
            "usd": selected["usd"],
            "handle": "/world/perception/" + output_path.name,
            "source_hash": selected["source_hash"],
            "retention_until": retention_until,
        }
        output_path.write_text(canonical(result), encoding="utf-8")
        self.db.execute(
            "UPDATE perception_quotes SET state='complete',result=? WHERE quote_id=?",
            (canonical(result), selected["quote_id"]),
        )
        self.store.append_record(
            run_id,
            "perception_call",
            action_id=action_id,
            provider=selected["provider"],
            quote_id=selected["quote_id"],
            usd=selected["usd"],
            retention_until=retention_until,
        )
        return result

    async def recover(self):
        # Never repeat an ambiguously billed vendor request after a process restart.
        self.db.execute("UPDATE perception_quotes SET state='unknown' WHERE state='processing'")

    def prune_expired(self, now: datetime):
        if now.tzinfo is None:
            raise ValueError("Retention sweeps require an aware world time")
        pruned = 0
        for row in self.db.execute(
            "SELECT * FROM perception_quotes WHERE state='complete'"
        ).fetchall():
            result = json.loads(row["result"])
            if datetime.fromisoformat(result["retention_until"]) > now:
                continue
            root = (self.store.run_dir(row["run_id"]) / "world" / "perception").resolve()
            path = self.store.run_dir(row["run_id"]) / result["handle"].lstrip("/")
            if path.resolve().is_relative_to(root) and path.resolve() == path:
                path.unlink(missing_ok=True)
            retained = {
                "source_hash": result["source_hash"],
                "output_hash": digest(result),
                "quote_id": row["quote_id"],
                "retention_until": result["retention_until"],
                "retention_expired": True,
            }
            self.db.execute(
                "UPDATE perception_quotes SET state='expired',result=? WHERE quote_id=?",
                (canonical(retained), row["quote_id"]),
            )
            pruned += 1
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"pruned": pruned, "store": "perception"}

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
        await super().aclose()
