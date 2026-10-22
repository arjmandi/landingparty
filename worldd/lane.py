"""Provider-compatible model lane with reservations and exact usage accounting.

Provider roots (without ``/v1``) are configured by WORLD_OPENAI_UPSTREAM and
WORLD_ANTHROPIC_UPSTREAM. Keys come from request headers or WORLD_*_API_KEY;
they are never recorded. Requests identify their run with X-World-Run-Id,
X-Run-Id, the run_id query parameter, or a /lane/{run_id}/... URL.

Streaming Chat calls must request stream_options.include_usage. All calls
must supply a maximum output token count. A conservative full-context input
reservation bounds cost even when the provider fetches remote images. Holds
are released only after complete, validated usage; interrupted/ambiguous calls
taint and freeze the run and retain their hold for operator reconciliation.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import hmac
import ipaddress
import json
import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from worldd.storage import Store


class UsageError(ValueError):
    """Usage cannot be priced without inventing a provider billing number."""


class _ActiveCall:
    def __init__(self, run_id, abort, close):
        self.run_id = run_id
        self.abort = abort
        self.close = close
        self.stop = asyncio.Event()
        self.done = asyncio.Event()


class LaneManager:
    """Drain lane calls before a control plane writes the final seal.

    ``stop_run`` first signals readers to stop. A downstream consumer may be
    paused while the lane yields an SSE chunk, so a bounded fallback closes the
    upstream and finalizes unknown-usage auditing synchronously. After it returns
    no model-call audit for an existing request can be appended after the seal.
    The caller must set the run's state to ``stopping`` first to reject new calls.
    """

    def __init__(self):
        self._active: set[_ActiveCall] = set()

    def register(self, run_id, *, abort, close):
        call = _ActiveCall(run_id, abort, close)
        self._active.add(call)
        return call

    def complete(self, call):
        call.done.set()
        self._active.discard(call)

    def active_count(self, run_id=None):
        return sum(run_id is None or call.run_id == run_id for call in self._active)

    async def stop_run(self, run_id, timeout=1.5):
        calls = [call for call in self._active if call.run_id == run_id]
        for call in calls:
            call.stop.set()
        if not calls:
            return {"active_before": 0, "active_remaining": 0, "audited": True}
        waiters = {asyncio.create_task(call.done.wait()): call for call in calls}
        _, pending = await asyncio.wait(waiters, timeout=max(0, timeout / 2))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        remaining = [waiters[task] for task in pending if not waiters[task].done.is_set()]
        for call in remaining:
            # Finalization guards make all later handler audit attempts no-ops.
            call.abort()
        if remaining:
            closers = [asyncio.create_task(call.close()) for call in remaining]
            _, pending_close = await asyncio.wait(closers, timeout=max(0, timeout / 3))
            for task in pending_close:
                task.cancel()
            await asyncio.gather(*closers, return_exceptions=True)
        return {
            "active_before": len(calls),
            "active_remaining": sum(not call.done.is_set() for call in calls),
            "audited": True,
        }


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_prices() -> dict:
    configured = os.getenv("WORLD_PRICE_TABLE")
    source = Path(configured) if configured else Path(__file__).parents[1] / "policy/prices.json"
    return json.loads(source.read_text())


WORLD_TOOL_PRICES = {
    "search_usd_per_call": "0.005",
    "exa_search_usd_per_call": "0.005",
    "browser_usd_per_hour": "0.12",
    "public_fetch_usd": "0",
}


def price_table_id(prices: dict | None = None, *, perception_prices=None) -> str:
    if perception_prices is None:
        from .perception import default_pricing_identity

        perception_prices = default_pricing_identity()
    return _hash(
        json.dumps(
            {
                "provider_prices": prices if prices is not None else load_prices(),
                "world_tool_prices": WORLD_TOOL_PRICES,
                "perception_prices": perception_prices,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError(f"Missing or invalid provider usage field: {name}")
    return value


def normalize_usage(provider: str, usage: dict) -> dict[str, int]:
    """Return disjoint token classes (cached tokens are not counted twice)."""
    if not isinstance(usage, dict):
        raise UsageError("Provider did not return usage")
    if provider == "anthropic":
        normal = {
            "input": _count(usage.get("input_tokens"), "input_tokens"),
            "output": _count(usage.get("output_tokens"), "output_tokens"),
            "cache_read": _count(
                usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"
            ),
            "cache_write": _count(
                usage.get("cache_creation_input_tokens", 0), "cache_creation_input_tokens"
            ),
            "cache_write_1h": 0,
        }
        split = usage.get("cache_creation")
        if split is not None:
            if not isinstance(split, dict):
                raise UsageError("Provider cache creation details must be an object")
            short = _count(split.get("ephemeral_5m_input_tokens", 0), "ephemeral_5m_input_tokens")
            long = _count(split.get("ephemeral_1h_input_tokens", 0), "ephemeral_1h_input_tokens")
            if short + long != normal["cache_write"]:
                raise UsageError("Cache duration breakdown does not equal cache creation usage")
            normal["cache_write"], normal["cache_write_1h"] = short, long
        return normal
    input_key = "prompt_tokens" if "prompt_tokens" in usage else "input_tokens"
    output_key = "completion_tokens" if "completion_tokens" in usage else "output_tokens"
    total_input = _count(usage.get(input_key), input_key)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        raise UsageError("Provider input token details must be an object")
    cached = _count(details.get("cached_tokens", 0), "cached_tokens")
    if cached > total_input:
        raise UsageError("Cached tokens exceed total input tokens")
    return {
        "input": total_input - cached,
        "output": _count(usage.get(output_key), output_key),
        "cache_read": cached,
        "cache_write": 0,
        "cache_write_1h": 0,
    }


def usage_cost(usage: dict[str, int], price: dict) -> Decimal:
    total = Decimal(0)
    for token_class, count in usage.items():
        count = _count(count, token_class)
        if count and token_class not in price:
            raise UsageError(f"No pinned price for token class {token_class}")
        try:
            rate = Decimal(str(price.get(token_class, "0")))
        except InvalidOperation as exc:
            raise UsageError("Invalid pinned token rate") from exc
        if not rate.is_finite() or rate < 0:
            raise UsageError("Invalid pinned token rate")
        total += Decimal(count) * rate / Decimal(1_000_000)
    return total


def _maximum_cost(body: dict, price: dict) -> Decimal:
    maximum = body.get(
        "max_completion_tokens", body.get("max_output_tokens", body.get("max_tokens"))
    )
    maximum = _count(maximum, "max_tokens/max_completion_tokens/max_output_tokens")
    if maximum == 0 or maximum > int(price["max_output_tokens"]):
        raise UsageError("Requested output token limit is outside the pinned model limits")
    # Reserving the full declared input window also bounds server-loaded media.
    input_limit = _count(price.get("context_window"), "price.context_window")
    rates = [
        Decimal(str(price.get(key, 0)))
        for key in ("input", "cache_read", "cache_write", "cache_write_1h")
    ]
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise UsageError("Invalid pinned token rate")
    return (
        Decimal(input_limit) * max(rates) + Decimal(maximum) * Decimal(str(price["output"]))
    ) / Decimal(1_000_000)


def _hosted_web_tools(body: dict) -> bool:
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and str(tool.get("type", "")).lower().startswith(("web_search", "web_fetch"))
        for tool in tools
    )


def _perception(body: Any) -> bool:
    if isinstance(body, dict):
        kind = str(body.get("type", ""))
        if any(word in kind for word in ("image", "audio", "video")):
            return True
        return any(_perception(value) for value in body.values())
    return isinstance(body, list) and any(_perception(value) for value in body)


class StreamUsage:
    """Incrementally parse SSE without changing bytes sent to the harness."""

    def __init__(self, provider: str):
        self.provider = provider
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.buffer = ""
        self.lines: list[str] = []
        self.usage: dict | None = None
        self.complete = False
        self.model: str | None = None

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        self.buffer += self.decoder.decode(chunk, final=final)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._line(line.rstrip("\r"))
        if final:
            if self.buffer:
                self._line(self.buffer.rstrip("\r"))
                self.buffer = ""
            self._line("")

    def _line(self, line: str) -> None:
        if line.startswith("data:"):
            self.lines.append(line[5:].lstrip())
        elif not line and self.lines:
            payload, self.lines = "\n".join(self.lines), []
            if payload == "[DONE]":
                self.complete = True
                return
            try:
                item = json.loads(payload)
            except (ValueError, TypeError):
                return
            if not isinstance(item, dict):
                return
            if self.provider == "anthropic":
                if item.get("type") == "message_start":
                    self.usage = dict(item.get("message", {}).get("usage", {}))
                    self.model = item.get("message", {}).get("model")
                elif item.get("type") == "message_delta" and isinstance(item.get("usage"), dict):
                    self.usage = {**(self.usage or {}), **item["usage"]}
                elif item.get("type") == "message_stop":
                    self.complete = True
            elif item.get("type") in ("response.completed", "response.incomplete"):
                self.complete = True
                self.usage = item.get("response", {}).get("usage")
                self.model = item.get("response", {}).get("model")
            elif isinstance(item.get("usage"), dict):
                self.usage = item["usage"]
                self.model = item.get("model")


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"type": kind, "message": message}})


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key.lower()
            in {
                "authorization",
                "x-api-key",
                "api_key",
                "password",
                "access_token",
                "refresh_token",
            }
            else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def create_lane_router(
    store: Store,
    *,
    client: httpx.AsyncClient | None = None,
    prices: dict | None = None,
    perception_prices=None,
    internal_token: str | None = None,
    capabilities=None,
    usage_observer=None,
    admission_guard=None,
) -> APIRouter:
    """Create a lane sharing the control plane's durable ledger."""
    router = APIRouter()
    manager = LaneManager()
    router.lane_manager = manager
    price_table = prices if prices is not None else load_prices()
    models = price_table.get("models", price_table)
    table_id = price_table_id(price_table, perception_prices=perception_prices)

    async def forward(request: Request, provider: str, endpoint: str, run_id: str | None = None):
        run_id = (
            run_id
            or request.headers.get("x-world-run-id")
            or request.headers.get("x-run-id")
            or request.query_params.get("run_id")
            or os.getenv("WORLD_RUN_ID")
        )
        if not run_id:
            return _error(400, "missing_run_id", "Provide X-World-Run-Id or a run-scoped lane URL")
        internal = bool(
            internal_token
            and hmac.compare_digest(
                request.headers.get("x-world-internal-token", ""), internal_token
            )
        )
        front_bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        frontend_auth = bool(capabilities and capabilities.verify(run_id, front_bearer))
        valid_run_token = bool(
            frontend_auth
            or (
                capabilities
                and capabilities.verify(run_id, request.headers.get("x-world-run-token", ""))
            )
        )
        if (
            getattr(request.app.state, "require_run_token", False)
            and not internal
            and not valid_run_token
        ):
            return _error(401, "run_capability", "Valid run capability required")
        try:
            run = store.get_run(run_id)
        except (KeyError, ValueError):
            return _error(404, "unknown_run", "No such run")
        if run.get("state") != "running":
            return _error(409, "run_not_running", "This run cannot make model calls")
        if admission_guard is not None:
            denial = admission_guard(run_id)
            if denial:
                return _error(409, denial["rule_id"], denial["detail"])
        requested_tag = request.headers.get("x-world-call-tag")
        if requested_tag and (
            not internal
            or requested_tag not in {"model.reflect", "model.reflector", "model.perception"}
        ):
            return _error(403, "call_tag", "World-side model tags require internal authorization")
        category = run.get("config", {}).get("metering_lane", "harness")
        category = category if category in {"judge", "probe", "kernel"} else "model"
        policy = run.get("config", {}).get("breaker_policy", {})
        minute_cap = policy.get("dollars_per_minute")
        if minute_cap is not None:
            cutoff = datetime.now(UTC).timestamp() - 60
            recent_spend = sum(
                float(r.get("cost_usd", 0))
                for r in store.records(run_id)
                if r["kind"] == "ledger_debit"
                and datetime.fromisoformat(r["ts"]).timestamp() >= cutoff
            )
            if recent_spend >= float(minute_cap):
                if policy.get("mode") == "log_only":
                    store.append_record(
                        run_id,
                        "breaker_would_intervene",
                        reason="dollars_per_minute",
                        mode="log_only",
                    )
                else:
                    store.update_run(run_id, state="frozen")
                    store.append_record(run_id, "intervention", reason="dollars_per_minute")
                    return _error(429, "spend_rate", "Dollars-per-minute guard reached")
        wall = run.get("config", {}).get("budget", {}).get("wall_s")
        created = run.get("activated_at", run.get("created_at"))
        if wall and created:
            try:
                if datetime.now(UTC).timestamp() >= datetime.fromisoformat(
                    created
                ).timestamp() + float(wall):
                    return _error(402, "wall_clock", "Run wall clock exhausted")
            except (ValueError, TypeError):
                return _error(500, "invalid_run_clock", "Run start time is invalid")
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_request", "Expected a JSON object")
        if not isinstance(body, dict):
            return _error(400, "invalid_request", "Expected a JSON object")
        model = body.get("model")
        if _hosted_web_tools(body):
            store.append_record(
                run_id,
                "control_plane_audit",
                decision="refused",
                rule_id="server_side_web_tool",
                provider=provider,
                request_hash=_hash(raw),
            )
            return _error(
                403,
                "server_side_web_tool",
                "Hosted web_search and web_fetch tools bypass the recorded boundary",
            )
        if (
            not isinstance(model, str)
            or model not in models
            or models[model].get("provider", provider) != provider
        ):
            store.append_record(
                run_id,
                "model_refused",
                rule_id="unpriced_model",
                provider=provider,
                request_hash=_hash(raw),
            )
            return _error(
                400, "unpriced_model", "Select an exact model snapshot in the pinned price table"
            )
        price = models[model]
        if (
            body.get("stream")
            and endpoint == "/v1/chat/completions"
            and not (body.get("stream_options") or {}).get("include_usage")
        ):
            return _error(
                400,
                "stream_usage_required",
                "Streaming Chat requires stream_options.include_usage=true",
            )
        # Extended-context and priority/flex rates require separate registered tables.
        if body.get("service_tier") not in (None, "default"):
            return _error(
                400, "unpriced_service_tier", "Only the pinned default service tier is priced"
            )
        if "context-1m" in request.headers.get("anthropic-beta", ""):
            return _error(
                400, "unpriced_context_tier", "Extended-context pricing is not in this price table"
            )
        try:
            maximum = _maximum_cost(body, price)
        except (UsageError, KeyError, InvalidOperation) as exc:
            return _error(400, "invalid_token_limit", str(exc))
        reservation = store.reserve(run_id, maximum)
        if reservation is None:
            store.append_record(
                run_id,
                "model_refused",
                rule_id="budget_exhausted",
                provider=provider,
                model=model,
                reservation_usd=float(maximum),
            )
            return _error(
                402,
                "budget_exhausted",
                "Remaining budget cannot cover this call's maximum possible spend",
            )

        root = os.getenv(
            f"WORLD_{provider.upper()}_UPSTREAM",
            "https://api.openai.com" if provider == "openai" else "https://api.anthropic.com",
        ).rstrip("/")
        url = root + endpoint
        secret = os.getenv(f"WORLD_{provider.upper()}_API_KEY")
        outgoing_headers = {"content-type": "application/json"}
        if provider == "openai":
            auth = (None if frontend_auth else request.headers.get("authorization")) or (
                f"Bearer {secret}" if secret else None
            )
            if auth:
                outgoing_headers["authorization"] = auth
        else:
            auth = request.headers.get("x-api-key") or secret
            if auth:
                outgoing_headers["x-api-key"] = auth
            if request.headers.get("authorization") and not frontend_auth:
                outgoing_headers["authorization"] = request.headers["authorization"]
            outgoing_headers["anthropic-version"] = request.headers.get(
                "anthropic-version", "2023-06-01"
            )
            if request.headers.get("anthropic-beta"):
                outgoing_headers["anthropic-beta"] = request.headers["anthropic-beta"]
        secrets = [
            str(secret or ""),
            str(auth or "").removeprefix("Bearer "),
            request.headers.get("authorization", "").removeprefix("Bearer "),
        ]
        own_client = client is None
        upstream_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=15), follow_redirects=False
        )
        upstream = urlsplit(root)
        host = upstream.hostname or ""
        private_origin = (
            upstream.scheme != "https"
            or "." not in host
            or host.endswith((".test", ".invalid", ".localhost", ".internal", ".local"))
        )
        try:
            private_origin |= not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        transport = upstream_client._transport
        fixture_transport = (
            True
            if private_origin or isinstance(transport, (httpx.MockTransport, httpx.ASGITransport))
            else False
            if isinstance(transport, httpx.AsyncHTTPTransport)
            else None
        )
        upstream_provenance = {
            "upstream_origin": f"{upstream.scheme}://{host}",
            "fixture_transport": fixture_transport,
            "transport_type": type(transport).__name__,
        }
        started = time.monotonic()
        call_id = "m_" + uuid.uuid4().hex
        finished = False
        response: httpx.Response | None = None
        captured = bytearray()
        capture_enabled = bool(run.get("config", {}).get("capture_model_io"))
        capture_saved = False

        def uncertain(reason: str) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            # Do not invent a usage or settle a guessed amount.
            current = store.get_run(run_id)
            changes = {"tainted": True}
            if current.get("state") == "running":
                changes["state"] = "frozen"
            store.update_run(run_id, **changes)
            store.append_record(
                run_id,
                "model_call",
                call_id=call_id,
                provider=provider,
                model=model,
                usage=None,
                **upstream_provenance,
                usd=None,
                reservation_id=reservation,
                reservation_usd=float(maximum),
                error=reason,
                request_hash=_hash(raw),
                price_table_id=table_id,
                latency_ms=round((time.monotonic() - started) * 1000),
                tags=[requested_tag]
                if requested_tag
                else (["model.perception"] if _perception(body) else []),
            )
            save_capture()

        def finish(usage: dict | None, observed_model: str | None = None) -> bool:
            nonlocal finished
            if finished:
                return False
            try:
                if observed_model is not None and observed_model != model:
                    raise UsageError(
                        "Provider returned a model different from the pinned request snapshot"
                    )
                normal = normalize_usage(provider, usage)
                if (
                    provider == "anthropic"
                    and normal["cache_write"]
                    and '"ttl": "1h"' in json.dumps(body)
                    and not (usage or {}).get("cache_creation")
                ):
                    raise UsageError(
                        "One-hour cache writes require the provider duration breakdown"
                    )
                cost = usage_cost(normal, price)
                if cost > maximum or not store.settle(reservation, category, cost):
                    raise UsageError("Provider usage exceeded the reserved maximum")
            except (UsageError, ValueError, InvalidOperation) as exc:
                uncertain(str(exc))
                return False
            finished = True
            store.append_record(
                run_id,
                "model_call",
                call_id=call_id,
                provider=provider,
                model=model,
                usage=normal,
                **upstream_provenance,
                reservation_id=reservation,
                usd=float(cost),
                request_hash=_hash(raw),
                price_table_id=table_id,
                latency_ms=round((time.monotonic() - started) * 1000),
                tags=[requested_tag]
                if requested_tag
                else (["model.perception"] if _perception(body) else []),
                spent_after=store.spend(run_id),
            )
            save_capture()
            if usage_observer is not None:
                try:
                    usage_observer(
                        run_id=run_id, actual_tokens=sum(normal.values()), call_id=call_id
                    )
                except Exception:  # noqa: BLE001 - metering remains authoritative if an observer fails.
                    store.update_run(run_id, tainted=True)
                    store.append_record(
                        run_id,
                        "control_plane_audit",
                        reason="usage_observer_fault",
                        call_id=call_id,
                    )
            return True

        def save_capture() -> None:
            nonlocal capture_saved
            if not capture_enabled or capture_saved:
                return
            if store.get_run(run_id)["state"] in {"sealed", "ended", "killed"}:
                return
            directory = store.run_dir(run_id) / "model-io"
            directory.mkdir(mode=0o700, exist_ok=True)
            target = directory / f"{call_id}.json"
            output = {
                "request": _redact(body, secrets),
                "response": _redact(captured.decode("utf-8", errors="replace"), secrets),
            }
            serialized = json.dumps(output, ensure_ascii=False).encode("utf-8")
            target.write_bytes(serialized)
            target.chmod(0o600)
            store.append_record(
                run_id,
                "model_capture",
                call_id=call_id,
                provider=provider,
                model=model,
                capture_ref=f"model-io/{call_id}.json",
                capture_hash=_hash(serialized),
                response_hash=_hash(captured),
                request_hash=_hash(raw),
            )
            capture_saved = True

        async def close_upstream() -> None:
            if response is not None:
                await response.aclose()
            if own_client:
                await upstream_client.aclose()

        active = manager.register(
            run_id,
            abort=lambda: uncertain("Run stopped before terminal provider usage was available"),
            close=close_upstream,
        )

        async def watch_run() -> None:
            while store.get_run(run_id).get("state") == "running" and not active.stop.is_set():
                try:
                    await asyncio.wait_for(active.stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass

        async def interruptible(awaitable):
            operation = asyncio.ensure_future(awaitable)
            watcher = asyncio.create_task(watch_run())
            try:
                if active.stop.is_set() or store.get_run(run_id).get("state") != "running":
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    raise UsageError("Run stopped during an upstream model call")
                done, _ = await asyncio.wait(
                    {operation, watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if operation in done:
                    return operation.result()
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise UsageError("Run stopped during an upstream model call")
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        try:
            response = await interruptible(
                upstream_client.send(
                    upstream_client.build_request(
                        "POST", url, content=raw, headers=outgoing_headers
                    ),
                    stream=True,
                )
            )
            if response.status_code >= 300:
                # Provider errors are explicitly not successful billable calls.
                await interruptible(response.aread())
                if not finished:
                    store.release(reservation)
                    finished = True
                    store.append_record(
                        run_id,
                        "model_refused",
                        provider=provider,
                        model=model,
                        upstream_status=response.status_code,
                        request_hash=_hash(raw),
                    )
                error_content = _redact(response.content.decode("utf-8", errors="replace"), secrets)
                # Redirects are refused instead of following credentials to a new origin.
                if 300 <= response.status_code < 400:
                    return _error(502, "upstream_redirect", "Provider redirects are not followed")
                return Response(
                    content=error_content,
                    status_code=response.status_code,
                    media_type="application/json",
                )
            if not body.get("stream"):
                content = await interruptible(response.aread())
                if capture_enabled:
                    captured.extend(content)
                try:
                    result = json.loads(content)
                except ValueError:
                    uncertain("Provider returned an invalid JSON body")
                    return _error(
                        502,
                        "unmetered_response",
                        "Provider response could not be metered; run frozen for reconciliation",
                    )
                if not isinstance(result, dict) or not finish(
                    result.get("usage"), result.get("model")
                ):
                    if not finished:
                        uncertain("Provider response is not an object")
                    return _error(
                        502,
                        "unmetered_response",
                        "Provider usage could not be metered; run frozen for reconciliation",
                    )
                return Response(
                    content=content, status_code=response.status_code, media_type="application/json"
                )

            async def stream():
                parser = StreamUsage(provider)
                try:
                    iterator = response.aiter_bytes().__aiter__()
                    while True:
                        try:
                            chunk = await interruptible(iterator.__anext__())
                        except StopAsyncIteration:
                            break
                        parser.feed(chunk)
                        if capture_enabled:
                            captured.extend(chunk)
                        yield chunk
                    parser.feed(b"", final=True)
                    if not parser.complete or not finish(parser.usage, parser.model):
                        uncertain("Provider stream ended without terminal usage")
                        yield b'event: error\ndata: {"error":{"type":"unmetered_response","message":"Run frozen for usage reconciliation"}}\n\n'
                except (httpx.HTTPError, httpx.StreamError, UsageError, UnicodeDecodeError):
                    uncertain("Provider stream failed or run stopped before terminal usage")
                    yield b'event: error\ndata: {"error":{"type":"upstream_stream_error","message":"Run frozen for usage reconciliation"}}\n\n'
                finally:
                    if not finished:
                        uncertain("Consumer disconnected before terminal usage")
                    try:
                        save_capture()
                        await close_upstream()
                    finally:
                        manager.complete(active)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        except httpx.ConnectError:
            if not finished:
                store.release(reservation)
                finished = True
                store.append_record(
                    run_id,
                    "model_refused",
                    provider=provider,
                    model=model,
                    rule_id="provider_connect_error",
                    request_hash=_hash(raw),
                )
            return _error(
                502, "provider_connect_error", "Could not connect to the configured provider"
            )
        except (httpx.HTTPError, httpx.StreamError, UsageError):
            uncertain("Provider request failed or run stopped before usage was available")
            return _error(
                502,
                "unmetered_response",
                "Provider usage is unknown; run frozen for reconciliation",
            )
        finally:
            if not body.get("stream") or response is None or response.status_code >= 300:
                if not finished:
                    uncertain("Model call ended before usage was recorded")
                try:
                    save_capture()
                    await close_upstream()
                finally:
                    manager.complete(active)

    async def chat(request: Request, run_id: str | None = None):
        return await forward(request, "openai", "/v1/chat/completions", run_id)

    async def responses(request: Request, run_id: str | None = None):
        return await forward(request, "openai", "/v1/responses", run_id)

    async def messages(request: Request, run_id: str | None = None):
        return await forward(request, "anthropic", "/v1/messages", run_id)

    for prefix in ("", "/openai", "/lane/{run_id}/openai"):
        router.add_api_route(prefix + "/v1/chat/completions", chat, methods=["POST"])
        router.add_api_route(prefix + "/v1/responses", responses, methods=["POST"])
    for prefix in ("", "/anthropic", "/lane/{run_id}/anthropic"):
        router.add_api_route(prefix + "/v1/messages", messages, methods=["POST"])
    return router
