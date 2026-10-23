"""Model spend estimated from a native subscription session's own transcript.

A subscription session talks to its provider directly, outside the model lane,
so its calls cannot be priced before they happen. A life on a subscription
still lives on a balance: this meter reads the usage the client itself reports,
prices it at the run's pinned API prices with the lane's own functions, and
debits it as `model.estimated`. Estimated dollars are their own measurement and
are labelled as such wherever they appear.
"""

from __future__ import annotations

import asyncio
import json
from decimal import ROUND_FLOOR, Decimal

from .lane import UsageError, normalize_usage, usage_cost

CATEGORY = "model.estimated"
# The lane refuses a call it cannot cover; after the fact, the meter stops the
# session once what is left would not cover another message like its largest.
MINIMUM_LOOKAHEAD = Decimal("0.01")
NANO = Decimal("0.000000001")
# Claude Code's per-model totals, in its own field names, as Anthropic usage.
MODEL_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
}


def _money(value) -> Decimal:
    return Decimal(str(value))


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
            "estimated": Decimal(0),
            "largest": Decimal(0),
            "closed": False,
        }
        for record in self.store.records(run_id):
            if record["kind"] == "model_estimate":
                state["messages"][record["message_id"]] = {
                    "usage": record["usage"],
                    "usd": _money(record["usd_total"]),
                }
            elif record["kind"] in {"estimate_unavailable", "model_refused"} and (
                record.get("metering") == "transcript"
            ):
                state["closed"] = True
        state["estimated"] = sum((item["usd"] for item in state["messages"].values()), Decimal(0))
        state["largest"] = max(
            (item["usd"] for item in state["messages"].values()), default=Decimal(0)
        )
        self.state[run_id] = state
        return state

    def metered(self, run_id) -> bool:
        return self.store.get_run(run_id)["config"].get("model_metering") == "transcript"

    # -- observation

    def observe(self, run_id: str, line: str) -> None:
        """Read one complete stdout line of a transcript-metered session."""
        run = self.store.get_run(run_id)
        if run["config"].get("model_metering") != "transcript":
            return
        if run["state"] not in {"running", "frozen"}:
            return
        state = self._state(run_id)
        if state["closed"]:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return  # Not a stream event; Claude's usage is always on a JSON line.
        if not isinstance(event, dict):
            return
        try:
            if event.get("type") == "assistant":
                self._message(run_id, run["config"], state, event.get("message"))
            elif event.get("type") == "result":
                self._true_up(run_id, run["config"], state, event)
        except (UsageError, KeyError, TypeError, ValueError) as error:
            self._fail(run_id, state, str(error)[:300])

    def overflow(self, run_id: str) -> None:
        """A line too long to read whole cannot be priced; the session stops."""
        if self.metered(run_id) and not self._state(run_id)["closed"]:
            self._fail(run_id, self._state(run_id), "transcript line too long to price")

    def _price(self, config, model):
        table = config.get("model_pricing") or {}
        models = table.get("models", table)
        price = models.get(model) if isinstance(model, str) else None
        if not price or price.get("provider") != "anthropic":
            raise UsageError(f"No pinned Anthropic price for model {model!r}")
        return price

    def _message(self, run_id, config, state, message):
        if not isinstance(message, dict) or not message.get("id"):
            raise UsageError("An assistant event without its message identity")
        if "usage" not in message:
            raise UsageError("An assistant message without its usage")
        model = message.get("model")
        price = self._price(config, model)
        reported = normalize_usage("anthropic", message["usage"])
        previous = state["messages"].get(message["id"])
        # The same message repeats across content chunks; its usage only grows.
        usage = (
            {key: max(value, previous["usage"].get(key, 0)) for key, value in reported.items()}
            if previous
            else reported
        )
        total = usage_cost(usage, price)
        delta = total - (previous["usd"] if previous else Decimal(0))
        if delta <= 0:
            return
        charged = self._charge(run_id, delta)
        state["messages"][message["id"]] = {"usage": usage, "usd": total}
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
        """Price the client's own per-model totals; charge only what messages missed."""
        totals = event.get("modelUsage")
        if not isinstance(totals, dict) or not totals:
            return
        total = Decimal(0)
        for model, fields in totals.items():
            if not isinstance(fields, dict):
                raise UsageError("Malformed per-model usage totals")
            usage = {name: fields.get(field, 0) for field, name in MODEL_USAGE_FIELDS.items()}
            total += usage_cost(normalize_usage("anthropic", usage), self._price(config, model))
        shortfall = total - state["estimated"]
        if shortfall <= 0:
            return
        charged = self._charge(run_id, shortfall)
        state["estimated"] += shortfall
        self.store.append_record(
            run_id,
            "model_estimate",
            message_id="result:" + str(event.get("session_id") or "session"),
            model="per-model totals",
            usage={},
            usd_total=str(shortfall),
            charged_usd=str(charged),
            metering="transcript",
        )

    # -- money and stopping

    def _left(self, run_id) -> Decimal:
        row = self.store._row(run_id)
        held = self.store._held_nano(run_id)
        return Decimal(row["cap_nano"] - row["spent_nano"] - held) / Decimal(1_000_000_000)

    def _charge(self, run_id, amount: Decimal) -> Decimal:
        """Debit up to what is left; record any estimate the cap could not take."""
        left = max(Decimal(0), self._left(run_id))
        charge = min(amount, left).quantize(NANO, rounding=ROUND_FLOOR)
        if charge > 0 and not self.store.debit(run_id, CATEGORY, charge):
            charge = Decimal(0)
        if amount > charge:
            self.store.append_record(
                run_id, "estimate_overshoot", usd=str(amount - charge), metering="transcript"
            )
        return charge

    def _lookahead(self, run_id, state):
        left = self._left(run_id)
        threshold = max(state["largest"], MINIMUM_LOOKAHEAD)
        if left > threshold:
            return
        state["closed"] = True
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
        state["closed"] = True
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
