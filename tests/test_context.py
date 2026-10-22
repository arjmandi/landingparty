"""Context claims require anchored exchange evidence and an exact current ask."""

import hashlib
import json

import pytest
from capability_helpers import CapabilityWorld

from worldd.context import ask_provenance


@pytest.fixture
def world(tmp_path):
    value = CapabilityWorld(tmp_path)
    yield value
    value.close()


def source(world, run_id, identifier, score):
    world.store.append_record(
        run_id,
        "outcome",
        action_id=identifier,
        reply={
            "kind": "result",
            "handle": "/world/fetched/" + identifier + ".html",
            "provenance": {"injection_score": score},
        },
    )


def capture(world, run_id, request, response, *, tags=None):
    count = sum(row["kind"] == "model_call" for row in world.store.records(run_id))
    call_id = "m_fixture_" + str(count)
    world.store.append_record(
        run_id,
        "model_call",
        call_id=call_id,
        provider="fixture",
        model="fixture",
        usage={"input": 5},
        request_hash="sha256:actual",
        tags=tags or [],
    )
    directory = world.store.run_dir(run_id) / "model-io"
    directory.mkdir(exist_ok=True)
    path = directory / (call_id + ".json")
    raw = json.dumps({"request": request, "response": response}).encode()
    path.write_bytes(raw)
    world.store.append_record(
        run_id,
        "model_capture",
        call_id=call_id,
        provider="fixture",
        model="fixture",
        request_hash="sha256:actual",
        capture_ref="model-io/" + path.name,
        capture_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
    return path


def chat_response(params, *, name="ask"):
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(params)},
                            }
                        ]
                    }
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "protocol", ["chat", "anthropic", "responses", "chat_sse", "anthropic_sse", "responses_sse"]
)
def test_verified_context_uses_structured_visible_refs_and_actual_source_scores(world, protocol):
    run_id = world.run()
    source(world, run_id, "a_used", 0.2)
    source(world, run_id, "a_pruned", 0.9)
    params = {"text": "Please help"}
    payload = json.dumps(
        {
            "handle": "/world/fetched/a_used.html",
            "body_preview": 'a_pruned {"action_id":"a_pruned"}',
            "site_result": {"action_id": "a_pruned"},
        }
    )
    if protocol.startswith("anthropic"):
        request = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "provider_tool_id",
                            "content": [{"type": "text", "text": payload}],
                        }
                    ],
                }
            ]
        }
        response = json.dumps(
            {"content": [{"type": "tool_use", "name": "mcp__world__ask", "input": params}]}
        )
    elif protocol.startswith("responses"):
        request = {
            "input": [
                {"type": "function_call_output", "call_id": "provider_tool_id", "output": payload}
            ]
        }
        response = json.dumps(
            {"output": [{"type": "function_call", "name": "ask", "arguments": json.dumps(params)}]}
        )
    else:
        request = {
            "messages": [
                {"role": "tool", "content": payload},
                {"role": "user", "content": json.dumps({"action_id": "a_pruned"})},
            ]
        }
        response = chat_response(params)
    if protocol == "chat_sse":
        arguments = json.dumps(params)
        response = "".join(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": part}]}}
                    ]
                }
            )
            + "\n\n"
            for part in [{"name": "ask", "arguments": arguments[:9]}, {"arguments": arguments[9:]}]
        )
    elif protocol == "anthropic_sse":
        response = "".join(
            "data: " + json.dumps(packet) + "\n\n"
            for packet in [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "name": "mcp__world__ask", "input": {}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(params)},
                },
            ]
        )
    elif protocol == "responses_sse":
        result = json.loads(response)
        response = "".join(
            "data: " + json.dumps(packet) + "\n\n"
            for packet in [
                {"type": "response.output_item.done", "item": result["output"][0]},
                {"type": "response.completed", "response": result},
            ]
        )
    capture(world, run_id, request, response)
    result = ask_provenance(world, run_id, {"kind": "message", **params})
    assert result["context_status"] == "verified"
    assert result["observation_ids"] == ["a_used"]
    assert result["injection_score_max"] == 0.2
    assert result["broker_observed_ids"] == ["a_pruned", "a_used"]
    assert result["broker_injection_score_max"] == 0.9


def test_uncaptured_or_unmatched_context_never_claims_broker_observations_as_visible(world):
    run_id = world.run()
    source(world, run_id, "a_observed", 0.8)
    assert (
        ask_provenance(world, run_id, {"text": "Help"})["context_reason"]
        == "no_ordinary_model_call"
    )
    capture(world, run_id, {"messages": []}, chat_response({"text": "A different ask"}))
    result = ask_provenance(world, run_id, {"text": "Help"})
    assert result["context_status"] == "unmeasured" and result["observation_ids"] == []
    assert result["broker_observed_ids"] == ["a_observed"] and result["injection_score_max"] is None


def test_duplicate_ask_match_is_ambiguous_and_capture_tamper_invalidates_claim(world):
    run_id = world.run()
    response = json.loads(chat_response({"text": "Help"}))
    response["choices"] *= 2
    path = capture(world, run_id, {"messages": []}, json.dumps(response))
    assert ask_provenance(world, run_id, {"text": "Help"})["context_status"] == "ambiguous"
    path.write_bytes(path.read_bytes() + b" ")
    assert (
        ask_provenance(world, run_id, {"text": "Help"})["context_reason"] == "capture_hash_mismatch"
    )


def test_latest_ordinary_call_required_reflection_excluded_agent_claim_ignored(world):
    run_id = world.run()
    source(world, run_id, "a_used", 0.3)
    request = {"messages": [{"role": "tool", "content": '{"action_id":"a_used"}'}]}
    capture(world, run_id, request, chat_response({"text": "Help"}))
    capture(world, run_id, {"messages": []}, "{}", tags=["model.reflect"])
    assert ask_provenance(world, run_id, {"text": "Help"})["observation_ids"] == ["a_used"]
    capture(world, run_id, request, chat_response({"text": "Different"}))
    assert ask_provenance(world, run_id, {"text": "Help"})["context_status"] == "unmeasured"
    assert (
        ask_provenance(
            world, run_id, {"text": "Different", "provenance": {"context_status": "verified"}}
        )["context_status"]
        == "unmeasured"
    )


async def test_actual_ask_ticket_records_verified_context(world):
    run_id = world.run()
    source(world, run_id, "a_used", 0.2)
    source(world, run_id, "a_pruned", 0.9)
    world.store.append_record(
        run_id,
        "action",
        action_id="a_login",
        tool="fetch",
        params={"url": "https://fixture.test/login"},
    )
    params = {"kind": "grant_credential", "text": "Please provide access", "action_ref": "a_login"}
    capture(
        world,
        run_id,
        {"messages": [{"role": "tool", "content": '{"action_id":"a_used"}'}]},
        chat_response(params),
    )
    result = await world.services["asks"].handle(run_id, "a_ask", "ask", params)
    ticket = world.services["asks"].get(result["ticket_id"])
    assert ticket["provenance"]["context_status"] == "verified"
    assert ticket["provenance"]["observation_ids"] == ["a_used"]
    assert ticket["provenance"]["broker_injection_score_max"] == 0.9
