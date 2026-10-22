"""Evidence for ask context from immutable, broker-captured model exchanges.

Broker-observed outcomes are distinct from outcomes in a verified model window.
Tool result metadata is parsed structurally; prose is never searched for IDs.
"""

from __future__ import annotations

import hashlib
import json
import math

from .models import AskParams

ASK_NAMES = {"ask", "mcp__world__ask"}
REF_KEYS = {"action_id", "event_id", "handle"}
ENVELOPES = {"reply", "resource", "block", "events", "results", "resources", "result"}


def _json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _refs(value):
    found = set()
    if isinstance(value, list):
        for item in value:
            found.update(_refs(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in REF_KEYS and isinstance(item, str) and len(item) <= 4096:
                found.add(item)
            elif key in ENVELOPES:
                found.update(_refs(item))
    return found


def _scores(value):
    scores = []
    if isinstance(value, dict):
        provenance = value.get("provenance")
        score = provenance.get("injection_score") if isinstance(provenance, dict) else None
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            scores.append(score)
        for key in ("reply", "resource", "block", "event"):
            scores.extend(_scores(value.get(key)))
    return scores


def _sources(records):
    result = []
    for row in records:
        if row["kind"] == "outcome":
            identifier = row.get("action_id")
            aliases = {identifier} if identifier else set()
            for key in ("resource", "reply", "block"):
                aliases.update(_refs(row.get(key)))
        elif row["kind"] == "event_pushed":
            event = row.get("event", {})
            identifier = event.get("event_id")
            aliases = _refs(event)
        else:
            continue
        if identifier:
            result.append({"identifier": identifier, "refs": aliases, "scores": _scores(row)})
    return result


def _payloads(value):
    if isinstance(value, str):
        parsed = _json(value)
        if isinstance(parsed, (dict, list)):
            yield parsed
    elif isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for part in value:
            if isinstance(part, dict) and part.get("type") in {"text", "output_text", "input_text"}:
                yield from _payloads(part.get("text"))


def _request_refs(request):
    refs = set()
    for message in request.get("messages", []):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            for payload in _payloads(message.get("content")):
                refs.update(_refs(payload))
        elif isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for payload in _payloads(block.get("content")):
                        refs.update(_refs(payload))
    inputs = request.get("input", [])
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                for payload in _payloads(item.get("output")):
                    refs.update(_refs(payload))
    return refs


def _calls(packet):
    if not isinstance(packet, dict):
        return []
    result = []
    for choice in packet.get("choices", []):
        message = choice.get("message", {})
        result.extend(message.get("tool_calls", []))
        if message.get("function_call"):
            result.append({"function": message["function_call"]})
    for block in packet.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            result.append(
                {"function": {"name": block.get("name"), "arguments": block.get("input")}}
            )
    for item in packet.get("output", []):
        if isinstance(item, dict) and item.get("type") == "function_call":
            result.append(
                {"function": {"name": item.get("name"), "arguments": item.get("arguments")}}
            )
    return result


def _response_calls(response):
    parsed = _json(response)
    if isinstance(parsed, dict):
        return _calls(parsed)
    if not isinstance(response, str):
        return []
    calls, fragments = [], {}
    for event in response.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        packet = _json(data)
        if not isinstance(packet, dict):
            continue
        calls.extend(_calls(packet))
        if packet.get("type") == "response.output_item.done":
            calls.extend(_calls({"output": [packet.get("item")]}))
        elif packet.get("type") == "response.completed":
            # A completed Responses object is authoritative; avoid counting its
            # earlier output_item.done event as another matching ask.
            return _calls(packet.get("response", {}))
        if packet.get("type") == "content_block_start":
            block = packet.get("content_block", {})
            if block.get("type") == "tool_use":
                fragments[("anthropic", packet.get("index"))] = {
                    "name": block.get("name"),
                    "arguments": "",
                    "input": block.get("input"),
                }
        elif packet.get("type") == "content_block_delta":
            key = ("anthropic", packet.get("index"))
            delta = packet.get("delta", {})
            if key in fragments and delta.get("type") == "input_json_delta":
                fragments[key]["arguments"] += delta.get("partial_json", "")
        for choice in packet.get("choices", []):
            delta = choice.get("delta", {})
            for part in delta.get("tool_calls", []):
                key = ("chat", choice.get("index", 0), part.get("index", 0))
                target = fragments.setdefault(key, {"name": "", "arguments": ""})
                function = part.get("function", {})
                target["name"] += function.get("name", "")
                target["arguments"] += function.get("arguments", "")
            if delta.get("function_call"):
                target = fragments.setdefault(
                    ("legacy", choice.get("index", 0)), {"name": "", "arguments": ""}
                )
                target["name"] += delta["function_call"].get("name", "")
                target["arguments"] += delta["function_call"].get("arguments", "")
    for fragment in fragments.values():
        calls.append(
            {
                "function": {
                    "name": fragment["name"],
                    "arguments": fragment["arguments"] or fragment.get("input"),
                }
            }
        )
    return calls


def _normalized(value):
    return AskParams.model_validate(_json(value)).model_dump(exclude_none=True, by_alias=True)


def ask_provenance(world, run_id, params=None):
    records = world.store.records(run_id)
    sources = _sources(records)
    base = {
        "observation_ids": [],
        "injection_score_max": None,
        "context_status": "unmeasured",
        "broker_observed_ids": sorted({item["identifier"] for item in sources}),
        "broker_injection_score_max": max(
            (score for item in sources for score in item["scores"]), default=None
        ),
    }

    def missing(reason, *, ambiguous=False):
        return {
            **base,
            "context_status": "ambiguous" if ambiguous else "unmeasured",
            "context_reason": reason,
        }

    if params is None:
        return missing("current_ask_arguments_not_available")
    calls = [
        (index, row)
        for index, row in enumerate(records)
        if row["kind"] == "model_call"
        and not {"model.reflect", "model.reflector"}.intersection(row.get("tags", []))
    ]
    if not calls:
        return missing("no_ordinary_model_call")
    index, call = calls[-1]
    if call.get("error") or call.get("usage") is None:
        return missing("latest_model_call_incomplete")
    anchors = [
        row
        for row in records
        if row["kind"] == "model_capture" and row.get("call_id") == call.get("call_id")
    ]
    if len(anchors) != 1:
        return missing("model_capture_anchor_missing_or_ambiguous", ambiguous=len(anchors) > 1)
    anchor = anchors[0]
    if not world.store.verify_record(run_id).get("valid"):
        return missing("record_chain_invalid")
    expected_ref = "model-io/" + str(call.get("call_id")) + ".json"
    if (
        anchor.get("capture_ref") != expected_ref
        or anchor.get("request_hash") != call.get("request_hash")
        or any(anchor.get(key) != call.get(key) for key in ("provider", "model"))
    ):
        return missing("capture_binding_mismatch")
    path = world.store.run_dir(run_id) / expected_ref
    try:
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            return missing("capture_unavailable_or_outside_scope")
        raw = path.read_bytes()
        if anchor.get("capture_hash") != "sha256:" + hashlib.sha256(raw).hexdigest():
            return missing("capture_hash_mismatch")
        capture = json.loads(raw)
        wanted = _normalized(params)
        matching = []
        for item in _response_calls(capture.get("response")):
            function = item.get("function", {})
            if function.get("name") not in ASK_NAMES:
                continue
            try:
                if _normalized(function.get("arguments")) == wanted:
                    matching.append(item)
            except (ValueError, TypeError):
                continue
        if len(matching) != 1:
            return missing("current_ask_not_uniquely_matched", ambiguous=len(matching) > 1)
        request = capture.get("request")
        if not isinstance(request, dict):
            return missing("captured_request_invalid")
        refs = _request_refs(request)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return missing("capture_protocol_invalid")
    selected = [item for item in _sources(records[:index]) if item["refs"].intersection(refs)]
    return {
        **base,
        "context_status": "verified",
        "context_reason": "unique_latest_ask_response",
        "context_call_id": call["call_id"],
        "context_capture_ref": expected_ref,
        "context_capture_hash": anchor["capture_hash"],
        "observation_ids": sorted({item["identifier"] for item in selected}),
        "context_refs": sorted(
            refs.intersection({ref for item in selected for ref in item["refs"]})
        ),
        "injection_score_max": max(
            (score for item in selected for score in item["scores"]), default=None
        ),
    }
