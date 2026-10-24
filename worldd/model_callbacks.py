"""Trusted internal model calls through the same reservation and metering lane."""

import json
import os
from decimal import Decimal

import httpx

from .lane import _maximum_cost, load_prices


def configure_models(app, world, *, prices=None):
    table = prices if prices is not None else load_prices()
    models = table.get("models", table)

    async def reflection_call(*, run_id, messages, tag, max_output_tokens, max_usd, purpose):
        from .beliefs import BeliefValidationError

        config = world.run_config(run_id)
        structuring = "structur" in purpose
        if config.get("research_mode") == "research" and config.get("reflection_policy") == "self":
            reasoning_id = config.get("reflection_model") or os.environ.get(
                "WORLD_REFLECTION_MODEL"
            )
            structuring_id = config.get("reflection_structuring_model")
            reasoning_price = models.get(reasoning_id, {})
            structuring_price = models.get(structuring_id, {})
            cheaper = (
                reasoning_price
                and structuring_price
                and structuring_id != reasoning_id
                and all(
                    Decimal(str(structuring_price[key])) <= Decimal(str(reasoning_price[key]))
                    for key in ("input", "output")
                )
                and any(
                    Decimal(str(structuring_price[key])) < Decimal(str(reasoning_price[key]))
                    for key in ("input", "output")
                )
            )
            if not cheaper:
                raise BeliefValidationError(
                    "Research self-reflection requires a distinct cheaper pinned structuring model"
                )
        chosen = (
            config.get("reflection_structuring_model")
            if structuring
            else config.get("reflection_model")
        ) or os.environ.get("WORLD_REFLECTION_MODEL")
        if chosen not in models:
            raise BeliefValidationError("Reflection requires a configured pinned model")
        price = models[chosen]
        provider = price["provider"]
        body = {"model": chosen, "messages": messages, "max_tokens": max_output_tokens}
        # Plain text: no token is shorter than a byte, so the request's size bounds its input.
        size = len(json.dumps(body, ensure_ascii=False).encode())
        if _maximum_cost(body, price, input_tokens=size) > Decimal(str(max_usd)):
            raise BeliefValidationError(
                "Reflection maximum reservation exceeds its remaining share"
            )
        path = "/openai/v1/chat/completions"
        if provider == "anthropic":
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            body["system"] = system
            body["messages"] = [m for m in messages if m["role"] != "system"]
            path = "/anthropic/v1/messages"
        before = {
            r.get("call_id") for r in world.store.records(run_id) if r["kind"] == "model_call"
        }
        headers = {
            "x-world-run-id": run_id,
            "x-world-internal-token": world.internal_model_token,
            "x-world-call-tag": tag,
        }
        control = os.environ.get("WORLD_CONTROL_TOKEN")
        if control:
            headers["x-world-control-token"] = control
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://world"
        ) as client:
            response = await client.post(path, json=body, headers=headers)
        if response.status_code >= 400:
            raise BeliefValidationError("Metered reflection provider request failed")
        result = response.json()
        if provider == "anthropic":
            text = "\n".join(part.get("text", "") for part in result.get("content", []))
        else:
            text = result["choices"][0]["message"].get("content", "")
        calls = [
            r
            for r in world.store.records(run_id)
            if r["kind"] == "model_call"
            and r.get("call_id") not in before
            and tag in r.get("tags", [])
        ]
        if not calls or calls[-1].get("usd") is None:
            raise BeliefValidationError("No settled reflection usage record")
        return {"text": text, "call_id": calls[-1]["call_id"]}

    if "reflection" in world.services:
        world.services["reflection"].callback = reflection_call
    if "pipeline" in world.services:
        from analysis.judges import MeteredResearchClient

        world.services["pipeline"].model = MeteredResearchClient.for_app(world, app)
    if "judge_canaries" in world.services:
        from analysis.judges import MeteredResearchClient

        world.services["judge_canaries"].model = MeteredResearchClient.for_app(world, app)
        world.services["judge_canaries"]._owns_model = True
