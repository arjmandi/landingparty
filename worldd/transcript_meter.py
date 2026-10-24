"""Model spend estimated from a native subscription session's own transcript.

A subscription session talks to its provider directly, outside the model lane,
so its calls cannot be priced before they happen. A life on a subscription
still lives on a balance: this meter reads the usage the client itself reports,
prices it at the run's pinned API prices with the lane's own functions, and
debits it as `model.estimated`. Estimated dollars are their own measurement and
are labelled as such wherever they appear.

Claude Code reports each message at its start and, with partial messages, its
final usage in `message_delta`; the earlier `assistant` event carries an early,
partial output count. Every observation of a message only ever raises its usage.
A provider error (a model the plan does not serve, a plan limit, an outage) is
recorded as `model_error` for every native Claude session, metered or not.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

from .lane import UsageError, normalize_usage, tier_rates, usage_cost

CATEGORY = "model.estimated"
# The lane refuses a call it cannot cover; after the fact, the meter stops the
# session once what is left would not cover another message like its largest.
MINIMUM_LOOKAHEAD = Decimal("0.01")
# Claude Code's per-model totals, in its own field names, as Anthropic usage.
MODEL_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
}
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
# Claude Code's own placeholder for a message it wrote itself; it is not a model call.
SYNTHETIC = "<synthetic>"
STATES = {"running", "frozen", "stopping"}


def _money(value) -> Decimal:
    return Decimal(str(value))


def error_class(status) -> str:
    """What a provider error means for a life: hold, rest until reset, or back off."""
    if status == 429:
        return "plan_limit"
    if isinstance(status, int) and not isinstance(status, bool) and status >= 500:
        return "provider_error"
    if status in (403, 404):
        return "model_unavailable"
    return "request_rejected"


def _text(message) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def _split(raw: dict, previous: dict | None) -> dict:
    """Give a later usage report the 5-minute/1-hour cache split its start carried.

    `message_delta` repeats the cache-creation total without the duration split;
    its `iterations` may carry it, and otherwise the message's start did.
    """
    total = raw.get("cache_creation_input_tokens")
    if raw.get("cache_creation") is not None or not isinstance(total, int) or not total:
        return raw
    short = long = 0
    for iteration in raw.get("iterations") or []:
        creation = iteration.get("cache_creation") if isinstance(iteration, dict) else None
        if isinstance(creation, dict):
            short += creation.get("ephemeral_5m_input_tokens", 0)
            long += creation.get("ephemeral_1h_input_tokens", 0)
    if short + long != total and previous:
        before_short = previous["usage"]["cache_write"]
        before_long = previous["usage"]["cache_write_1h"]
        before = before_short + before_long
        long = total * before_long // before if before else 0
        short = total - long
    if short + long != total:
        return raw
    return {
        **raw,
        "cache_creation": {"ephemeral_5m_input_tokens": short, "ephemeral_1h_input_tokens": long},
    }


class TranscriptMeter:
    def __init__(self, world):
        self.world = world
        self.store = world.store
        self.state: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()

    # -- state, rebuilt from the run's own record so a restart never charges twice

    def _state(self, run_id):
        if run_id in self.state:
            return self.state[run_id]
        state = {
            "messages": {},
            # Summed usage per model, the base of the end-of-session true-up.
            "models": {},
            # Models seen paying a long-prompt tier in some message.
            "long": set(),
            # The message each stream is reporting, keyed by its parent tool use.
            "streams": {},
            "estimated": Decimal(0),
            "largest": Decimal(0),
            "resets_at": None,
            # The lookahead has asked the session to stop; usage it drains still counts.
            "stopping": False,
            # Usage could not be priced; nothing further can be counted honestly.
            "failed": False,
            # A provider error already ended this session.
            "errored": False,
        }
        for record in self.store.records(run_id):
            if record["kind"] == "model_estimate":
                self._remember(state, record["message_id"], record.get("model"), record["usage"])
                state["messages"][record["message_id"]]["usd"] = _money(record["usd_total"])
            elif record["kind"] == "model_refused" and record.get("metering") == "transcript":
                state["stopping"] = True
            elif record["kind"] == "estimate_unavailable":
                state["failed"] = True
            elif record["kind"] == "model_error":
                state["errored"] = True
        state["estimated"] = sum((item["usd"] for item in state["messages"].values()), Decimal(0))
        state["largest"] = max(
            (item["usd"] for item in state["messages"].values()), default=Decimal(0)
        )
        self.state[run_id] = state
        return state

    @staticmethod
    def _remember(state, message_id, model, usage):
        """Record a message's (grown) usage and add the growth to its model's sum."""
        previous = state["messages"].get(message_id)
        before = previous["usage"] if previous else {}
        totals = state["models"].setdefault(model, dict.fromkeys(TOKEN_CLASSES, 0))
        for name in TOKEN_CLASSES:
            totals[name] += usage.get(name, 0) - before.get(name, 0)
        state["messages"][message_id] = {
            "usage": usage,
            "usd": previous["usd"] if previous else Decimal(0),
            "model": model,
        }

    def metered(self, run_id) -> bool:
        return self.store.get_run(run_id)["config"].get("model_metering") == "transcript"

    # -- observation

    def observe(self, run_id: str, line: str) -> None:
        """Read one complete stdout line of a native session.

        Every session is watched for provider errors; only transcript-metered
        sessions are priced.
        """
        run = self.store.get_run(run_id)
        if run["state"] not in STATES:
            return
        metered = run["config"].get("model_metering") == "transcript"
        state = self._state(run_id)
        if state["failed"]:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return  # Not a stream event; Claude's usage is always on a JSON line.
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        try:
            if kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                resets = info.get("resetsAt") if isinstance(info, dict) else None
                if isinstance(resets, (int, float)) and not isinstance(resets, bool):
                    state["resets_at"] = resets
            elif kind == "assistant" and event.get("is_api_error_message"):
                self._model_error(
                    run_id,
                    run["config"],
                    state,
                    event.get("api_error_status"),
                    event.get("error"),
                    _text(event.get("message")),
                )
            elif (
                kind == "result"
                and event.get("is_error")
                and (event.get("terminal_reason") == "api_error")
            ):
                self._model_error(
                    run_id,
                    run["config"],
                    state,
                    event.get("api_error_status"),
                    None,
                    event.get("result") if isinstance(event.get("result"), str) else "",
                )
            if not metered:
                return
            if kind == "stream_event":
                self._stream(run_id, run["config"], state, event)
            elif kind == "assistant":
                self._message(run_id, run["config"], state, event.get("message"))
            elif kind == "result":
                self._true_up(run_id, run["config"], state, event)
        except (UsageError, KeyError, TypeError, ValueError) as error:
            if metered:
                self._fail(run_id, state, str(error)[:300])

    def overflow(self, run_id: str) -> None:
        """A line too long to read whole cannot be priced; the session stops."""
        if self.metered(run_id) and not self._state(run_id)["failed"]:
            self._fail(run_id, self._state(run_id), "transcript line too long to price")

    def _price(self, config, model):
        table = config.get("model_pricing") or {}
        models = table.get("models", table)
        price = models.get(model) if isinstance(model, str) else None
        if not price or price.get("provider") != "anthropic":
            raise UsageError(f"No pinned Anthropic price for model {model!r}")
        return price

    def _stream(self, run_id, config, state, event):
        """Partial messages: a message's start, then its final usage in its delta."""
        inner = event.get("event")
        if not isinstance(inner, dict):
            return
        stream = event.get("parent_tool_use_id")
        if inner.get("type") == "message_start":
            message = inner.get("message")
            self._message(run_id, config, state, message)
            state["streams"][stream] = (message["id"], message.get("model"))
        elif inner.get("type") == "message_delta":
            if stream not in state["streams"]:
                raise UsageError("A message delta without its message start")
            message_id, model = state["streams"][stream]
            self._message(
                run_id, config, state, {"id": message_id, "model": model, "usage": inner["usage"]}
            )

    def _message(self, run_id, config, state, message):
        if not isinstance(message, dict) or not message.get("id"):
            raise UsageError("An assistant event without its message identity")
        if "usage" not in message:
            raise UsageError("An assistant message without its usage")
        model = message.get("model")
        if model == SYNTHETIC:
            # Claude Code writes these itself (an error notice, an interruption).
            if any(normalize_usage("anthropic", message["usage"]).values()):
                raise UsageError("A synthetic message reported model usage")
            return
        price = self._price(config, model)
        previous = state["messages"].get(message["id"])
        reported = normalize_usage("anthropic", _split(message["usage"], previous))
        # The same message repeats across reports; its usage only grows.
        usage = (
            {key: max(value, previous["usage"].get(key, 0)) for key, value in reported.items()}
            if previous
            else reported
        )
        total = usage_cost(usage, price)
        if (
            price.get("long_context")
            and tier_rates(price, sum(usage[name] for name in TOKEN_CLASSES if name != "output"))
            is not price
        ):
            state["long"].add(model)
        delta = total - (previous["usd"] if previous else Decimal(0))
        if delta <= 0:
            return
        charged = self._charge(run_id, delta)
        self._remember(state, message["id"], model, usage)
        state["messages"][message["id"]]["usd"] = total
        state["estimated"] += delta
        state["largest"] = max(state["largest"], total)
        self.store.append_record(
            run_id,
            "model_estimate",
            message_id=message["id"],
            model=model,
            usage=usage,
            usd_total=str(total),
            charged_usd=str(charged),
            metering="transcript",
        )
        self._lookahead(run_id, state)

    def _true_up(self, run_id, config, state, event):
        """Price the client's own per-model totals; charge only the tokens messages missed.

        The totals carry no cache-duration split: the messages' own split is used,
        else the session's aggregate one. A tiered model's remainder is priced at the
        tier its messages paid, never at the tier of the summed session.
        """
        totals = event.get("modelUsage")
        if not isinstance(totals, dict) or not totals:
            return
        aggregate = (event.get("usage") or {}).get("cache_creation")
        session = str(event.get("session_id") or "session")
        for model, fields in totals.items():
            if not isinstance(fields, dict):
                raise UsageError("Malformed per-model usage totals")
            price = self._price(config, model)
            reported = normalize_usage(
                "anthropic",
                {name: fields.get(field, 0) for field, name in MODEL_USAGE_FIELDS.items()},
            )
            seen = state["models"].get(model, dict.fromkeys(TOKEN_CLASSES, 0))
            creation = reported["cache_write"]
            seen_creation = seen["cache_write"] + seen["cache_write_1h"]
            if seen_creation:
                long = creation * seen["cache_write_1h"] // seen_creation
            elif isinstance(aggregate, dict) and (
                both := aggregate.get("ephemeral_5m_input_tokens", 0)
                + aggregate.get("ephemeral_1h_input_tokens", 0)
            ):
                long = creation * aggregate.get("ephemeral_1h_input_tokens", 0) // both
            else:
                long = 0
            reported["cache_write"], reported["cache_write_1h"] = creation - long, long
            remainder = {name: max(0, reported[name] - seen[name]) for name in TOKEN_CLASSES}
            if not any(remainder.values()):
                continue
            rates = tier_rates(price, None if model in state["long"] else 0)
            amount = usage_cost(remainder, {k: v for k, v in rates.items() if k != "long_context"})
            if amount <= 0:
                continue
            charged = self._charge(run_id, amount)
            message_id = f"result:{session}:{model}"
            self._remember(state, message_id, model, remainder)
            state["messages"][message_id]["usd"] = amount
            state["estimated"] += amount
            self.store.append_record(
                run_id,
                "model_estimate",
                message_id=message_id,
                model=model,
                usage=remainder,
                usd_total=str(amount),
                charged_usd=str(charged),
                metering="transcript",
                true_up=True,
            )

    # -- provider errors

    def _model_error(self, run_id, config, state, status, error, text):
        """Record why the provider refused, once, and end the session.

        The record is appended before the session ends, so the life reads the cause
        from the record however the process exit races the stop.
        """
        if state["errored"]:
            return
        state["errored"] = True
        kind = error_class(status)
        detail = {
            "status": status if isinstance(status, int) else None,
            "error": error if isinstance(error, str) else None,
            "error_class": kind,
            "model": config.get("model_snapshot_id"),
            "message": (text or "")[:500],
        }
        if kind == "plan_limit" and state["resets_at"]:
            detail["resets_at"] = datetime.fromtimestamp(state["resets_at"], UTC).isoformat()
        self.store.append_record(run_id, "model_error", **detail)
        self._end(run_id, reason="model_error", truncated=False)

    # -- money and stopping

    def _left(self, run_id) -> Decimal:
        row = self.store._row(run_id)
        held = self.store._held_nano(run_id)
        return Decimal(row["cap_nano"] - row["spent_nano"] - held) / Decimal(1_000_000_000)

    def _charge(self, run_id, amount: Decimal) -> Decimal:
        """Charge up to what is left; record any estimate the cap could not take."""
        charge = self.store.charge_incurred(run_id, CATEGORY, amount)
        if amount > charge:
            self.store.append_record(
                run_id, "estimate_overshoot", usd=str(amount - charge), metering="transcript"
            )
        return charge

    def _lookahead(self, run_id, state):
        if state["stopping"]:
            return
        left = self._left(run_id)
        threshold = max(state["largest"], MINIMUM_LOOKAHEAD)
        if left > threshold:
            return
        state["stopping"] = True
        self.store.append_record(
            run_id,
            "model_refused",
            rule_id="budget_exhausted",
            metering="transcript",
            left_usd=str(left),
            lookahead_usd=str(threshold),
        )
        self._end(run_id, reason="budget_exhausted", truncated=False)

    def _fail(self, run_id, state, reason):
        """Unknown model spend is never zero: record it, taint the run, and stop."""
        state["failed"] = True
        self.store.append_record(
            run_id, "estimate_unavailable", reason=reason, metering="transcript"
        )
        self.store.update_run(run_id, tainted=True)
        self._end(run_id, reason="estimate_unavailable", truncated=True)

    def _end(self, run_id, *, reason, truncated):
        # Ending from inside the capture task would wait on its own drain.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.world.end_run(run_id, reason=reason, truncated=truncated))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def aclose(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
