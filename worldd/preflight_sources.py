"""World-side instrument artifacts derived from actual binaries and recorded receipts."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

from .extraction import EXTRACTOR_ID
from .preflight_probes import harness_command
from .util import canonical, digest, now


class SourceObserver:
    def __init__(self, service):
        self.service, self.world, self.store = service, service.world, service.store

    def _save(self, run_id, label, payload):
        payload = {
            "observer": "world",
            "observer_source": "world-preflight-sources@1",
            "observed_at": now(),
            **payload,
        }
        directory = self.store.run_dir(run_id) / "preflight/instruments"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (label + "-" + digest(payload).split(":", 1)[1] + ".json")
        if target.is_symlink():
            raise ValueError("Instrument artifacts must be private regular files")
        target.write_text(canonical(payload))
        target.chmod(0o600)
        self.store.append_record(
            run_id,
            "preflight_source",
            instrument=label,
            payload_hash=digest(payload),
            artifact_hash=digest(target.read_bytes()),
            status=payload.get("status"),
        )
        return {"path": str(target), "hash": digest(target.read_bytes())}

    def registered(self, run_id, label, payload):
        return any(
            record.get("kind") == "preflight_source"
            and record.get("instrument") == label
            and record.get("payload_hash") == digest(payload)
            for record in self.store.records(run_id)
        )

    async def browser_build(self, run_id):
        config = self.store.get_run(run_id)["config"]
        if "browse" not in config.get("packs", []):
            return {"applicable": False, "build": None, "status": "not_applicable"}
        backend = getattr(self.world.services.get("browser"), "backend", None)
        if backend is None or self.world.services["browser"]._injected_backend:
            return {
                "applicable": True,
                "build": None,
                "status": "unmeasured",
                "reason": "No production browser backend",
            }
        path = getattr(backend, "executable_path", None)
        manager = None
        try:
            if not path:
                from playwright.async_api import async_playwright

                manager = await async_playwright().start()
                path = manager.chromium.executable_path
            environment = backend.child_environment()
            process = await asyncio.create_subprocess_exec(
                path,
                "--version",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 10)
            except (TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                raise
            build = stdout.decode(errors="replace").strip()
            if process.returncode or not build or len(build) > 500:
                raise ValueError("Browser build observation failed")
            return {
                "applicable": True,
                "build": build,
                "status": "measured",
                "executable_hash": digest(Path(path).read_bytes()),
            }
        except (OSError, ValueError, TimeoutError):
            return {
                "applicable": True,
                "build": None,
                "status": "unmeasured",
                "reason": "Browser build command unavailable",
            }
        finally:
            if manager:
                await manager.stop()

    def _verified_run(self, run_id, *, sealed=False):
        run = self.store.get_run(run_id)
        if (
            run.get("tainted")
            or not self.store.verify_record(run_id)["valid"]
            or not self.store.reconcile(run_id)["valid"]
        ):
            raise ValueError("Instrument source record/ledger failed integrity")
        records = self.store.records(run_id)
        if sealed and (run["state"] != "sealed" or not records or records[-1]["kind"] != "seal"):
            raise ValueError("Instrument source must be sealed")
        return run, records

    def provider(self, run_id, source_id, call_id):
        from .lane import (
            StreamUsage,
            _hosted_web_tools,
            load_prices,
            normalize_usage,
            price_table_id,
            usage_cost,
        )

        _, records = self._verified_run(source_id, sealed=True)
        calls = [record for record in records if record["kind"] == "model_call"]
        call = next(record for record in calls if record.get("call_id") == call_id)
        capture = next(
            record
            for record in records
            if record["kind"] == "model_capture" and record.get("call_id") == call_id
        )
        path = self.store.run_dir(source_id) / capture["capture_ref"]
        if path.resolve() != path or not path.is_relative_to(
            self.store.run_dir(source_id) / "model-io"
        ):
            raise ValueError("Model capture must remain in its private directory")
        raw = path.read_bytes()
        if digest(raw) != capture["capture_hash"]:
            raise ValueError("Model capture failed its record hash")
        value = json.loads(raw)
        response = value["response"]
        request = value["request"]
        if request.get("stream"):
            parser = StreamUsage(call["provider"])
            parser.feed(response.encode(), final=True)
            if not parser.complete:
                raise ValueError("Provider stream has no terminal receipt")
            usage, model = parser.usage, parser.model
        else:
            response = json.loads(response)
            usage, model = response.get("usage"), response.get("model")
        normalized = normalize_usage(call["provider"], usage)
        prices = load_prices()
        config = self.store.get_run(run_id)["config"]
        if (
            price_table_id(prices, perception_prices=config.get("perception_pricing"))
            != self.world.price_table_id
        ):
            raise ValueError("The observer cannot read the active pinned price source")
        price = prices["models"][call["model"]]
        amount = usage_cost(normalized, price)
        if (
            normalized != call["usage"]
            or model != call["model"]
            or call["model"] != config.get("model_snapshot_id")
            or call is not calls[0]
            or _hosted_web_tools(request)
        ):
            raise ValueError("First provider receipt differs from the pinned metered request")
        return {
            "status": "measured",
            "provider_run_id": source_id,
            "call_id": call_id,
            "model_snapshot_id": model,
            "usage": normalized,
            "provider_response_hash": capture["response_hash"],
            "capture_hash": capture["capture_hash"],
            "metered_usd": str(call["usd"]),
            "provider_usage_price_usd": str(amount),
            "provider_reported_usd": None,
            "price_table_id": call["price_table_id"],
            "source_observed_at": call["ts"],
            "first_request_no_hosted_tools": True,
            "fixture_transport": call.get("fixture_transport"),
            "upstream_origin": call.get("upstream_origin"),
            "transport_type": call.get("transport_type"),
        }

    def search_date(self, source_id, action_id, provider):
        _, records = self._verified_run(source_id, sealed=True)
        actions = [
            record
            for record in records
            if record.get("kind") == "action"
            and record.get("tool") == "search"
            and record.get("action_id") == action_id
        ]
        if len(actions) != 1:
            raise ValueError("A recorded search action is required")
        boundary = next(
            record
            for record in records
            if record.get("kind") == "network_boundary"
            and record.get("action_id") == action_id
            and record.get("realm") == "search"
        )
        child_id = boundary["observation_run_id"]
        child, child_records = self._verified_run(child_id, sealed=True)
        entries = json.loads((self.store.run_dir(child_id) / "archive-index.json").read_text())
        hosts = {"brave": "api.search.brave.com", "exa": "api.exa.ai"}
        for entry in entries.values():
            reply = entry["reply"]
            from urllib.parse import urlsplit

            if (
                reply.get("status") != 200
                or urlsplit(reply["final_url"]).hostname != hosts[provider]
            ):
                continue
            headers = reply.get("headers_subset", {})
            outcomes = [
                record
                for record in child_records
                if record.get("kind") == "outcome"
                and record.get("resource_hash") == reply.get("hash")
                and record.get("status") == 200
            ]
            if (
                len(outcomes) != 1
                or outcomes[0].get("response_headers_subset") != headers
                or outcomes[0].get("response_headers_hash") != digest(headers)
            ):
                raise ValueError("Search response headers lack their sealed receipt anchor")
            path = self.store.run_dir(child_id) / entry["body_path"]
            if (
                path.resolve() != path
                or not path.is_relative_to(self.store.run_dir(child_id) / "private-responses")
                or digest(path.read_bytes()) != reply["hash"]
            ):
                raise ValueError("Search response body failed its private receipt hash")
            date = reply.get("headers_subset", {}).get("date")
            if not date:
                raise ValueError("Search provider receipt has no HTTP Date")
            parsed = parsedate_to_datetime(date)
            if parsed.tzinfo is None:
                raise ValueError("Search provider date has no timezone")
            return {
                "date": parsed.astimezone(UTC).isoformat(),
                "source_run_id": source_id,
                "action_id": action_id,
                "observation_run_id": child_id,
                "response_hash": reply["hash"],
                "fixture_transport": child["config"].get("fixture_transport", False),
            }
        raise ValueError("No matching actual search provider response")

    async def egress(self, run_id, url):
        result = await self.world.fetch_kernel(run_id, url, inline=True, max_inline_chars=2000)
        if result.get("kind") != "result":
            return {
                "status": "unmeasured",
                "ip": None,
                "reverse_dns": None,
                "rule_id": result.get("rule_id"),
                "source_url": url,
            }
        try:
            body = result.get("inline", result.get("preview", ""))
            try:
                parsed = json.loads(body)
                body = parsed.get("ip") if isinstance(parsed, dict) else parsed
            except ValueError:
                pass
            address = ipaddress.ip_address(str(body).strip())
            if not address.is_global:
                raise ValueError("Egress observer returned a nonpublic address")
            resolver = self.world.gateway._resolver
            if hasattr(resolver, "reverse"):
                reverse = await resolver.reverse(str(address))
            else:
                reverse = (
                    await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, str(address)), 3)
                )[0]
            return {
                "status": "measured",
                "ip": str(address),
                "reverse_dns": reverse,
                "source_url": url,
                "response_hash": result["hash"],
                "observation_run_id": self.world.kernel_runs[run_id],
            }
        except (OSError, ValueError, TypeError, TimeoutError):
            return {
                "status": "unmeasured",
                "ip": None,
                "reverse_dns": None,
                "source_url": url,
                "response_hash": result.get("hash"),
                "reason": "Public IP/PTR observation unavailable",
            }

    async def observe(
        self,
        run_id,
        *,
        provider_run_id=None,
        provider_call_id=None,
        search_run_id=None,
        search_action_id=None,
        egress_url=None,
    ):
        config = self.store.get_run(run_id)["config"]
        binding = self.service.harnesses.get(run_id, {})
        harness = None
        try:
            observation = binding.get("harness_observation")
            if observation is None and binding.get("execute"):
                observation = await self.service._call(
                    binding["execute"], harness_command(config.get("harness_id", "manual@0.1"))
                )
            if observation and observation.get("returncode") == 0:
                harness = json.loads(observation["stdout"])
        except (OSError, ValueError, TypeError):
            harness = None
        browser = await self.browser_build(run_id)
        signer = self.world.gateway.signer
        signed = config.get("browser_identity") == "signed"
        versions = {
            "status": "partial",
            "extractor_id": EXTRACTOR_ID,
            "harness_id": config.get("harness_id"),
            "harness_observation": harness,
            "harness_image_id": (
                self.service._host_bridge.accepted.get(run_id, {})
                .get("receipt", {})
                .get("structure", {})
                .get("agent_image_id")
                if self.service._host_bridge
                else None
            ),
            "model_snapshot_id": config.get("model_snapshot_id"),
            "price_table_id": self.world.price_table_id,
            "browser_build": browser["build"],
            "browser_observation": browser,
            "signing_key_id": signer.key_id if signer and signed else None,
            "signing_directory_hash": digest(signer.directory()) if signer and signed else None,
            "search_response_date": None,
            "search_observation": None,
        }
        if search_run_id and search_action_id:
            try:
                search = self.search_date(
                    search_run_id, search_action_id, config.get("search_provider", "brave")
                )
                versions.update(search_response_date=search["date"], search_observation=search)
                if harness and browser["status"] != "unmeasured":
                    versions["status"] = "measured"
            except (OSError, ValueError, KeyError, StopIteration, TypeError):
                versions["status"] = "partial"
        metadata = {"versions": self._save(run_id, "versions", versions)}
        canaries = self.world.services.get("canaries")
        if canaries is not None and hasattr(canaries, "profile_for_run"):
            profile = canaries.profile_for_run(run_id)
            if profile.get("provenance_verified") and profile.get("measurement_id"):
                path = canaries.root / (profile["measurement_id"] + ".json")
                metadata["block_profile"] = {"path": str(path), "hash": digest(path.read_bytes())}
        if provider_run_id and provider_call_id:
            try:
                provider = self.provider(run_id, provider_run_id, provider_call_id)
            except (OSError, ValueError, KeyError, StopIteration, TypeError):
                provider = {
                    "status": "unmeasured",
                    "reason": "Actual first provider capture is missing or invalid",
                }
            metadata["provider_canary"] = self._save(run_id, "provider_canary", provider)
        if egress_url:
            metadata["egress"] = self._save(run_id, "egress", await self.egress(run_id, egress_url))
        return {"run_id": run_id, "metadata": metadata}
