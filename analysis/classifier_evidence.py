"""Rebuild saved allocation labels from frozen instruments and actual captures."""

from __future__ import annotations

import json

from worldd.util import digest

from .allocation import (
    BOOLEAN_LABELS,
    INSTRUCTION,
    INSTRUMENT_ID,
    _agreement,
    _frame,
    _labels,
    _source,
    _summary,
)
from .comparisons import _family
from .instruments import load_artifact
from .judges import JudgeSpec, _json_reply
from .values import _receipt


def verify_saved_allocation(world, source_run_id, payload):
    """Ignore saved scores; reconstruct every label, calibration and metric.

    A hash-pinned scalar agreement is not calibration evidence. This checks the
    actual archived inputs, frozen expert labels, tool-less provider responses,
    child records and bills before a life rubric can consume any classifier V.
    """
    store = world.store
    source = _source(store, source_run_id)
    if (
        payload.get("instrument_id") != INSTRUMENT_ID
        or payload.get("source_run_id") != source_run_id
        or payload.get("source_record_head") != source["records"][-1]["hash"]
    ):
        raise ValueError("saved allocation does not bind the actual source record")
    raw, instrument_ref = load_artifact(store, source_run_id, payload["instrument_ref"])
    instrument = json.loads(raw)
    classes, calibration_rows = instrument["classes"], instrument.get("calibration_rows", [])
    if digest({"classes": classes, "calibration_rows": calibration_rows}) != payload.get(
        "protocol_hash"
    ):
        raise ValueError("saved allocation protocol differs from its frozen instrument")
    agent_model = store.get_run(source_run_id)["config"]["model_snapshot_id"]
    judge_family = _family(payload["model"], world)
    if _family(agent_model, world) == judge_family:
        raise ValueError("life classifier must use an actually independent model family")
    spec = JudgeSpec(model=payload["model"], family=judge_family, prompt_id=payload["prompt_id"])
    cached, seen, class_ids = {source_run_id: source}, set(), {row["id"] for row in classes}

    def replay(row, data):
        data["classes"] = classes
        data["instrument_hash"] = instrument_ref["hash"]
        if (
            row.get("input_hash") != digest(data)
            or row.get("through_seq") != data["through_seq"]
            or row.get("source_context_ref") != data["source_context_ref"]
        ):
            raise ValueError("saved allocation input differs from actual source prefix")
        child_id = row["measurement_run_id"]
        result = {"measurement": {"measurement_run_id": child_id, "spent": store.spend(child_id)}}
        _receipt(store, source_run_id, spec, result, seen=seen)
        calls = [r for r in store.records(child_id) if r["kind"] == "model_call" and r.get("usage")]
        if len(calls) != 1 or calls[0].get("model") != spec.model:
            raise ValueError("classifier receipt requires one actual matching model call")
        call = calls[0]
        anchors = [
            r
            for r in store.records(child_id)
            if r["kind"] == "model_capture" and r.get("call_id") == call["call_id"]
        ]
        name = "model-io/" + call["call_id"] + ".json"
        path = store.run_dir(child_id) / name
        if len(anchors) != 1 or anchors[0].get("capture_ref") != name or path.resolve() != path:
            raise ValueError("classifier needs its actual anchored prompt and response")
        body = path.read_bytes()
        if digest(body) != anchors[0].get("capture_hash") or anchors[0].get(
            "request_hash"
        ) != call.get("request_hash"):
            raise ValueError("classifier capture integrity failed")
        capture = json.loads(body)
        request, response = capture["request"], json.loads(capture["response"])
        messages = request.get("messages", [])
        users = [item for item in messages if item.get("role") == "user"]
        system = request.get("system", "") or "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        )
        if (
            request.get("model") != spec.model
            or request.get("tools") not in (None, [])
            or request.get("functions")
            or INSTRUCTION not in system
            or len(users) != 1
            or json.loads(users[0]["content"]) != data
        ):
            raise ValueError("classifier capture differs from the frozen tool-less source frame")
        if response.get("choices"):
            message = response["choices"][0]["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ValueError("classifier attempted a tool")
            text = message["content"]
        else:
            if any(item.get("type") == "tool_use" for item in response.get("content", [])):
                raise ValueError("classifier attempted a tool")
            text = "".join(
                item.get("text", "")
                for item in response.get("content", [])
                if item.get("type") == "text"
            )
        checked = _labels(_json_reply({"text": text}), data, class_ids)
        if row.get("spent_usd") != sum(store.spend(child_id).values()):
            raise ValueError("saved classifier cost differs from its actual ledger")
        return checked

    actual_calibration = payload.get("calibration_judgments", [])
    if len(actual_calibration) != len(calibration_rows):
        raise ValueError("saved allocation omitted frozen calibration judgments")
    rebuilt_calibration, identities = [], set()
    for index, (frozen, saved) in enumerate(zip(calibration_rows, actual_calibration, strict=True)):
        owner = frozen["input"].get("source_run_id", source_run_id)
        action = frozen["input"]["action_ref"]
        if (
            (owner, action) in identities
            or saved.get("index") != index
            or saved.get("truth") != frozen["labels"]
        ):
            raise ValueError("classifier expert slices must be distinct and preserve frozen labels")
        identities.add((owner, action))
        if owner not in cached:
            cached[owner] = _source(store, owner)
        family = source["run"]["config"].get("family")
        if cached[owner]["run"]["config"].get("family") != family or (
            source["run"]["config"].get("research_mode") == "research" and not family
        ):
            raise ValueError(
                "classifier expert calibration must match the actual source goal family"
            )
        checked = replay(saved, _frame(cached[owner], [action]))
        if saved.get("reading") != checked[0]:
            raise ValueError("saved calibration label differs from captured provider response")
        rebuilt_calibration.append({**saved, "reading": checked[0]})
    actions = source["actions"]
    segments = payload.get("segments", [])
    if len(segments) != (len(actions) + 24) // 25:
        raise ValueError("saved classifier omitted source action segments")
    readings, rebuilt_segments = [], []
    for index, saved in enumerate(segments):
        data = _frame(source, [row["action_id"] for row in actions[index * 25 : (index + 1) * 25]])
        checked = replay(saved, data)
        if saved.get("index") != index or saved.get("readings") != checked:
            raise ValueError("saved allocation readings differ from captured provider response")
        readings.extend(checked)
        rebuilt_segments.append({**saved, "readings": checked})
    agreements = {
        key: _agreement(rebuilt_calibration, key, pinned=True)
        for key in ("content_class", *BOOLEAN_LABELS)
    }
    for segment in rebuilt_segments:
        segment["metrics"] = _summary(segment["readings"], agreements, class_ids)
    return {
        **payload,
        "segments": rebuilt_segments,
        "calibration": agreements,
        "metrics": _summary(readings, agreements, class_ids),
        "receipt_integrity_verified": True,
        "instrument_ref": instrument_ref,
    }
