"""J-column Elo over actual blinded, swapped and length-controlled judgments."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from worldd.storage import content_hash
from worldd.workspaces import snapshot_manifest

from .judges import STYLE_CONTROL_INSTRUCTION, JudgeSpec, ResearchCallError, _json_reply
from .publication import load_measurements
from .report import unmeasured
from .values import _receipt


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _family(model, world):
    mapping = getattr(world, "model_families", {})
    configured = mapping.get(model)
    value = str(configured or model).lower()
    for prefix, family in (
        ("claude", "anthropic"),
        ("anthropic", "anthropic"),
        ("gpt", "openai"),
        ("openai", "openai"),
        ("o1", "openai"),
        ("o3", "openai"),
        ("o4", "openai"),
        ("gemini", "google"),
        ("google", "google"),
        ("deepseek", "deepseek"),
        ("llama", "meta"),
        ("qwen", "qwen"),
        ("mistral", "mistral"),
    ):
        if value.startswith(prefix):
            return family
    if configured or value in {str(item).lower() for item in mapping.values()}:
        return value
    raise ValueError("a verified model-family mapping is required")


def _participant(world, item):
    store = world.store
    run_id, reference = item["run_id"], item["path"]
    run, records = store.get_run(run_id), store.records(run_id)
    if (
        run["state"] != "sealed"
        or run.get("tainted")
        or not store.verify_record(run_id)["valid"]
        or not store.reconcile(run_id)["valid"]
    ):
        raise ValueError("comparison source is not sealed and verified")
    if store.reconcile(run_id).get("reserved_usd"):
        raise ValueError("comparison source has unresolved spend")
    root = store.run_dir(run_id)
    path = root / reference
    if not reference.startswith("work-snapshot/") or path.resolve() != path or not path.is_file():
        raise ValueError("comparison artifacts must be actual sealed workspace files")
    anchors = [row for row in records if row["kind"] == "workspace_snapshot"]
    if len(anchors) != 1 or item.get("snapshot_anchor") != anchors[0]["hash"]:
        raise ValueError("workspace snapshot anchor missing")
    if any(
        anchors[0].get(key) != value
        for key, value in snapshot_manifest(root / "work-snapshot").items()
    ):
        raise ValueError("workspace snapshot was modified")
    raw = path.read_bytes()
    if _hash(raw) != item.get("hash"):
        raise ValueError("comparison artifact hash mismatch")
    config = run["config"]
    for key in ("model_snapshot_id", "harness_id", "family", "tuple"):
        if item.get(key) != config.get(key):
            raise ValueError("participant metadata does not match its actual author")
    if item.get("goal_hash") != content_hash(config.get("goal", "")):
        raise ValueError("participant goal provenance differs")
    core = None
    if config.get("family") == "F5":
        measured = load_measurements(store, run_id)
        core = measured.get("grading", {}).get("checklist", {}).get("core_passed")
        if measured.get("_integrity", {}).get("valid") is not True or not isinstance(core, bool):
            raise ValueError("F5 comparison needs its verified frozen programmatic core")
    return {
        **item,
        "competitor_id": content_hash({"run_id": run_id, "path": reference, "hash": item["hash"]}),
        "source_record_head": records[-1]["hash"],
        "core_passed": core,
        "model_family": _family(config["model_snapshot_id"], world),
    }, raw.decode("utf-8")


def _judgment_capture(world, source_run_id, judgment, seen):
    spec = JudgeSpec(
        model=judgment["judge_id"], family=judgment["judge_family"], prompt_id=judgment["prompt_id"]
    )
    _receipt(world.store, source_run_id, spec, judgment, seen=seen)
    child_id = judgment["measurement"]["measurement_run_id"]
    records = world.store.records(child_id)
    calls = [row for row in records if row["kind"] == "model_call" and row.get("usage")]
    if len(calls) != 1:
        raise ValueError("a pairwise position needs exactly one actual model call")
    call = calls[0]
    if call.get("model") != judgment["judge_id"]:
        raise ValueError("judge call model differs from its receipt")
    anchors = [
        row
        for row in records
        if row["kind"] == "model_capture" and row.get("call_id") == call["call_id"]
    ]
    if len(anchors) != 1:
        raise ValueError("a pairwise position needs an anchored actual prompt/response capture")
    anchor = anchors[0]
    expected = "model-io/" + call["call_id"] + ".json"
    path = world.store.run_dir(child_id) / expected
    if anchor.get("capture_ref") != expected or path.resolve() != path or not path.is_file():
        raise ValueError("judge capture scope invalid")
    raw = path.read_bytes()
    if _hash(raw) != anchor.get("capture_hash") or anchor.get("request_hash") != call.get(
        "request_hash"
    ):
        raise ValueError("judge capture hash/binding invalid")
    capture = json.loads(raw)
    request, response = capture["request"], json.loads(capture["response"])
    if request.get("model") != judgment["judge_id"]:
        raise ValueError("captured judge model differs")
    if request.get("tools") not in (None, []) or request.get("functions"):
        raise ValueError("pairwise measurement was not tool-less")
    if response.get("choices"):
        message = response["choices"][0]["message"]
        if message.get("tool_calls") or message.get("function_call"):
            raise ValueError("pairwise response attempted a tool")
        text = message["content"]
    else:
        if any(item.get("type") == "tool_use" for item in response.get("content", [])):
            raise ValueError("pairwise response attempted a tool")
        text = "".join(
            item.get("text", "")
            for item in response.get("content", [])
            if item.get("type") == "text"
        )
    if (
        text != judgment["measurement"].get("text")
        or _json_reply({"text": text}) != judgment["judgment"]
    ):
        raise ValueError("pairwise verdict differs from its actual captured model response")
    messages = [item for item in request.get("messages", []) if item.get("role") == "user"]
    system = request.get("system", "") or "\n".join(
        str(item.get("content", ""))
        for item in request.get("messages", [])
        if item.get("role") == "system"
    )
    if STYLE_CONTROL_INSTRUCTION not in system:
        raise ValueError("actual pairwise prompt lacks its frozen style-control instruction")
    if len(messages) != 1:
        raise ValueError("pairwise prompt must be a single blinded data object")
    data = json.loads(messages[0]["content"])
    if set(data) != {"artifact_A", "artifact_B", "criterion"}:
        raise ValueError("pairwise judge data is not blinded to author provenance")
    return data, {
        "measurement_run_id": child_id,
        "source_record_head": records[-1]["hash"],
        "capture_hash": anchor["capture_hash"],
        "request_hash": call.get("request_hash"),
        "response_hash": anchor.get("response_hash"),
    }


def build_pairwise_elo(world, judge_refs):
    """Read manifest-bound pairwise artifacts, never caller-provided winners or scores."""
    if not isinstance(judge_refs, (list, tuple)):
        raise TypeError("Supply actual pairwise stage artifact descriptors")
    groups, excluded, seen, references = defaultdict(list), [], set(), set()
    for descriptor in judge_refs:
        try:
            source_run_id = descriptor["run_id"]
            measured = load_measurements(world.store, source_run_id)
            stage = measured.get("_manifest", {}).get("stages", {}).get("pairwise", {})
            if (
                measured.get("_integrity", {}).get("valid") is not True
                or descriptor.get("path") != stage.get("artifact")
                or descriptor.get("hash") != stage.get("hash")
            ):
                raise ValueError("pairwise descriptor is not the actual verified stage")
            identity = (source_run_id, stage["hash"])
            if identity in references:
                raise ValueError("duplicate pairwise evidence")
            references.add(identity)
            pair = measured["pairwise"]
            style = pair.get("style_control", {})
            if style != {
                "method": "judge_instruction",
                "instruction": STYLE_CONTROL_INSTRUCTION,
                "hash": content_hash(STYLE_CONTROL_INSTRUCTION),
            }:
                raise ValueError("verified style-control evidence missing")
            control = pair.get("length_control", {})
            if (
                pair.get("position_swap") is not True
                or control.get("method") != "equal_character_prefix"
                or type(control.get("chars")) is not int
                or control["chars"] <= 0
                or len(pair.get("judgments", [])) != 2
            ):
                raise ValueError("pairwise position and equal-length control evidence missing")
            left, a = _participant(world, pair["participants"]["left"])
            right, b = _participant(world, pair["participants"]["right"])
            if left["family"] != right["family"] or left["goal_hash"] != right["goal_hash"]:
                raise ValueError("different task families or goals cannot share an Elo contest")
            length = control["chars"]
            if min(len(a), len(b)) < length:
                raise ValueError("pairwise length control exceeds actual artifacts")
            judgments, receipts, outcomes, judge_ids, criteria = (
                pair["judgments"],
                [],
                [],
                set(),
                set(),
            )
            for index, judgment in enumerate(judgments):
                judge_family = _family(judgment["judge_id"], world)
                if judge_family != _family(judgment["judge_family"], world) or judge_family in {
                    left["model_family"],
                    right["model_family"],
                }:
                    raise ValueError(
                        "pairwise judge must be from another actual model family than both authors"
                    )
                data, receipt = _judgment_capture(world, source_run_id, judgment, seen)
                expected_a, expected_b = (
                    (a[:length], b[:length]) if index == 0 else (b[:length], a[:length])
                )
                if data["artifact_A"] != expected_a or data["artifact_B"] != expected_b:
                    raise ValueError("position swap/prefix differs from actual author artifacts")
                winner = judgment["judgment"].get("winner")
                if winner not in {"A", "B", "tie"}:
                    raise ValueError("invalid pairwise verdict")
                outcomes.append(
                    (
                        {"A": "left", "B": "right", "tie": "tie"}
                        if index == 0
                        else {"A": "right", "B": "left", "tie": "tie"}
                    )[winner]
                )
                receipts.append(receipt)
                judge_ids.add((judgment["judge_id"], judgment["prompt_id"]))
                criteria.add(data["criterion"])
            if len(judge_ids) != 1 or len(criteria) != 1:
                raise ValueError("positions used different judge or criterion")
            winner = outcomes[0] if outcomes[0] == outcomes[1] else "disagreement"
            if pair.get("winner") != winner:
                raise ValueError("pairwise aggregate winner contradicts actual position verdicts")
            common = {
                key: value
                for key, value in left["tuple"].items()
                if key
                not in {
                    "model_snapshot_id",
                    "model_value_document_id",
                    "harness_id",
                    "preamble_hash",
                }
            }
            other = {
                key: value
                for key, value in right["tuple"].items()
                if key
                not in {
                    "model_snapshot_id",
                    "model_value_document_id",
                    "harness_id",
                    "preamble_hash",
                }
            }
            if common != other:
                raise ValueError(
                    "comparison artifacts differ in world conditions beyond model/harness arms"
                )
            condition = {
                "family": left["family"],
                "goal_hash": left["goal_hash"],
                "tuple": common,
                "judge": sorted(judge_ids),
                "criterion": sorted(criteria),
                "length_control_method": control["method"],
            }
            groups[json.dumps(condition, sort_keys=True)].append(
                {
                    "source": descriptor,
                    "left": left,
                    "right": right,
                    "winner": winner,
                    "length_control": control,
                    "receipts": receipts,
                }
            )
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            ResearchCallError,
        ) as error:
            excluded.append({"source": descriptor, **unmeasured(str(error))})
    strata = []
    for condition, contests in sorted(groups.items()):
        ratings, players, matches = {}, {}, []
        for contest in sorted(
            contests, key=lambda item: (item["source"]["hash"], item["source"]["run_id"])
        ):
            left, right = contest["left"], contest["right"]
            for player in (left, right):
                key = player["competitor_id"]
                ratings.setdefault(key, 1500.0)
                players[key] = player
            a, b = left["competitor_id"], right["competitor_id"]
            reason = (
                "position_disagreement"
                if contest["winner"] == "disagreement"
                else "programmatic_core_bottom_cap"
                if False in (left["core_passed"], right["core_passed"])
                else "same_source_artifact"
                if a == b
                else None
            )
            applied = reason is None
            if applied:
                actual = {"left": 1.0, "right": 0.0, "tie": 0.5}[contest["winner"]]
                expected = 1 / (1 + 10 ** ((ratings[b] - ratings[a]) / 400))
                delta = 32 * (actual - expected)
                ratings[a] += delta
                ratings[b] -= delta
            matches.append({**contest, "rating_applied": applied, "unmeasured_reason": reason})
        ranking = [
            {
                **players[key],
                "style_elo": value
                if players[key]["core_passed"] is not False
                and any(
                    match["rating_applied"]
                    and key in {match["left"]["competitor_id"], match["right"]["competitor_id"]}
                    for match in matches
                )
                else None,
                "bracket": "bottom" if players[key]["core_passed"] is False else "eligible",
                "column": "J",
            }
            for key, value in sorted(ratings.items())
        ]
        ranking.sort(
            key=lambda item: (
                item["bracket"] == "bottom",
                -(item["style_elo"] or 0),
                item["competitor_id"],
            )
        )
        strata.append(
            {
                "conditions": json.loads(condition),
                "column": "J",
                "status": "measured"
                if any(item["rating_applied"] for item in matches)
                else "unmeasured",
                "rankings": ranking,
                "matches": matches,
            }
        )
    return {
        "status": "measured"
        if any(item["status"] == "measured" for item in strata)
        else "unmeasured",
        "column": "J",
        "headline": False,
        "method": {
            "name": "artifact Elo",
            "initial": 1500,
            "K": 32,
            "logistic_scale": 400,
            "order": "stage hash then source run ID",
            "ties": "actual tie in both swapped positions only",
            "disagreements": "unmeasured; no rating update",
            "core_rule": "failed F5 core remains bottom; no Elo update",
        },
        "strata": strata,
        "excluded": excluded,
        "counts": {
            "attempted": len(judge_refs),
            "accepted": sum(len(group["matches"]) for group in strata),
            "rating_applied": sum(
                match["rating_applied"] for group in strata for match in group["matches"]
            ),
            "excluded": len(excluded),
        },
    }
