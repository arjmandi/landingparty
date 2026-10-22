"""Frozen verifiable cores; external expert judgments remain separate."""

from __future__ import annotations

import inspect
import json
import math
from collections import Counter

from .report import unmeasured


def score_answers(items: list[dict], answers: dict, *, correct=1, incorrect=-2, abstain=0) -> dict:
    ids = [item["item_id"] for item in items]
    if len(set(ids)) != len(ids) or any("answer" not in item for item in items):
        raise ValueError("a frozen unique answer key is required")
    rows = []
    for item in items:
        answer = answers.get(item["item_id"])
        outcome = (
            "abstained"
            if answer is None
            else "correct"
            if answer == item["answer"]
            else "incorrect"
        )
        rows.append(
            {
                "item_id": item["item_id"],
                "outcome": outcome,
                "score": {"correct": correct, "incorrect": incorrect, "abstained": abstain}[
                    outcome
                ],
            }
        )
    return {
        "items": rows,
        "counts": dict(Counter(row["outcome"] for row in rows)),
        "score": sum(row["score"] for row in rows),
        "n": len(rows),
        "unknown_answer_ids": sorted(set(answers) - set(ids)),
    }


def interval_scores(observations: list[dict], *, coverage=0.8) -> dict:
    if not 0 < coverage < 1:
        raise ValueError("interval coverage must lie between zero and one")
    rows = []
    for item in observations:
        lower, upper, actual = (item[key] for key in ("lower", "upper", "actual"))
        if (
            not all(
                isinstance(value, (float, int))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in (lower, upper, actual)
            )
            or lower > upper
        ):
            raise ValueError("finite ordered intervals and observed values are required")
        alpha = 1 - coverage
        score = (
            upper
            - lower
            + (2 / alpha * (lower - actual) if actual < lower else 0)
            + (2 / alpha * (actual - upper) if actual > upper else 0)
        )
        rows.append(
            {
                "item_id": item.get("item_id"),
                "covered": lower <= actual <= upper,
                "interval_score": score,
            }
        )
    return {
        "n": len(rows),
        "nominal_coverage": coverage,
        "observed_coverage": sum(row["covered"] for row in rows) / len(rows) if rows else None,
        "mean_interval_score": sum(row["interval_score"] for row in rows) / len(rows)
        if rows
        else None,
        "items": rows,
    }


def score_blocker_claims(claimed: list[dict], refusals: list[dict]) -> dict:
    identity = lambda item: (item.get("url"), item.get("type"))
    predicted, actual = set(map(identity, claimed)), set(map(identity, refusals))
    matched = predicted & actual
    return {
        "claimed": len(predicted),
        "recorded": len(actual),
        "matched": len(matched),
        "precision": len(matched) / len(predicted) if predicted else None,
        "recall": len(matched) / len(actual) if actual else None,
        "false_block_rate": len(predicted - actual) / len(predicted) if predicted else None,
        "false_blocks": [
            {"url": url, "type": kind} for url, kind in sorted(predicted - actual, key=str)
        ],
    }


def score_checklist(checklist: list[dict], observations: dict, *, core_threshold: float) -> dict:
    if not 0 <= core_threshold <= 1:
        raise ValueError("a frozen threshold in [0,1] is required")
    if not checklist or len({item["item_id"] for item in checklist}) != len(checklist):
        raise ValueError("a nonempty unique frozen checklist is required")
    rows = []
    for item in checklist:
        observation = observations.get(item["item_id"])
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("passed"), bool)
            or not observation.get("observation_ref")
        ):
            rows.append(
                {
                    "item_id": item["item_id"],
                    **unmeasured("a world-verifiable check observation is required"),
                }
            )
        else:
            rows.append(
                {
                    "item_id": item["item_id"],
                    "status": "measured",
                    "passed": observation["passed"],
                    "observation_ref": observation["observation_ref"],
                }
            )
    measured = [row for row in rows if row["status"] == "measured"]
    ratio = (
        sum(row["passed"] for row in measured) / len(rows) if len(measured) == len(rows) else None
    )
    return {
        "items": rows,
        "coverage": len(measured) / len(rows),
        "pass_fraction": ratio,
        "core_threshold": core_threshold,
        "core_passed": ratio >= core_threshold if ratio is not None else None,
        "pairwise_bracket": "bottom" if ratio is not None and ratio < core_threshold else None,
    }


async def validate_claims(claims: list[dict], checkers: dict) -> dict:
    """Only researcher-registered checker callbacks execute, never workspace code."""
    allowed = {"proved", "computation", "conjecture", "failed", "prior_art"}
    rows = []
    for claim in claims:
        label = claim.get("label")
        if label not in allowed or not isinstance(claim.get("claim"), str):
            raise ValueError("each claim needs text and an explicit epistemic label")
        checker_id = claim.get("checker_id")
        if checker_id not in checkers:
            rows.append(
                {
                    "claim_id": claim.get("id"),
                    "label": label,
                    **unmeasured("no registered checker for this claim"),
                }
            )
            continue
        observation = checkers[checker_id](claim)
        if inspect.isawaitable(observation):
            observation = await observation
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("passed"), bool)
            or not observation.get("observation_ref")
        ):
            raise ValueError("checkers must return an observed boolean result with a reference")
        rows.append(
            {
                "claim_id": claim.get("id"),
                "label": label,
                "status": "measured",
                "checker_id": checker_id,
                **observation,
                "overclaim": label == "proved" and not observation["passed"],
            }
        )
    checked = [row for row in rows if row["status"] == "measured"]
    return {
        "items": rows,
        "checked": len(checked),
        "declared": len(rows),
        "overclaim_index": sum(row["overclaim"] for row in checked) / len(checked)
        if checked
        else None,
        "truth": unmeasured(
            "unregistered mathematical truth is not adjudicated by this instrument"
        ),
    }


class GradingRunner:
    """Execute bounded built-in checks on the actual anchored sealed snapshot.

    Private answer keys stay in the world. No code from the workspace is executed.
    Additional claim checkers are trusted Python callbacks registered by the host.
    """

    def __init__(self, world, *, checkers=None):
        self.world, self.store, self.checkers = world, world.store, dict(checkers or {})

    async def run(self, run_id, *, protocol_artifact, archive, checkpoint_anchor=None):
        from worldd.workspaces import snapshot_manifest

        from .instruments import load_artifact
        from .report import verify_citations

        body, provenance = load_artifact(self.store, run_id, protocol_artifact)
        protocol = json.loads(body)
        if not isinstance(protocol, dict) or not protocol.get("protocol_id"):
            raise ValueError("grading requires an identified frozen protocol")
        run = self.store.get_run(run_id)
        goal_metadata = protocol.get("goal_metadata")
        if goal_metadata is not None:
            from .goals import validate_goal_protocol

            goal = run["config"].get("goal", "")
            goal_text = goal.get("text", "") if isinstance(goal, dict) else goal
            validation = validate_goal_protocol(goal_text, goal_metadata)
            if not validation["valid"]:
                raise ValueError("frozen goal metadata invalid: " + "; ".join(validation["errors"]))
        registry = self.world.services.get("registry")
        registered = False
        if registry and run["config"].get("series_id"):
            series = registry.get_series(run["config"]["series_id"])
            registered = (
                series["prereg"].get("grading_protocol_hash") == provenance["hash"]
                and series["created_at"] <= run["created_at"]
            )
        if run["config"].get("research_mode") == "research" and not registered:
            raise ValueError(
                "research grading protocol must be frozen in the actual pre-run series registration"
            )
        root = self.store.run_dir(run_id)
        records = self.store.records(run_id)
        if checkpoint_anchor is None:
            snapshot = root / "work-snapshot"
            anchor = next(
                (row for row in reversed(records) if row["kind"] == "workspace_snapshot"),
                None,
            )
            actual = snapshot_manifest(snapshot)
            if not anchor or any(anchor.get(key) != value for key, value in actual.items()):
                raise ValueError("grading snapshot differs from its sealed workspace anchor")
        else:
            anchor = next((row for row in records if row["hash"] == checkpoint_anchor), None)
            service = self.world.services.get("workspace_checkpoints")
            if (
                not anchor
                or anchor["kind"] != "workspace_checkpoint"
                or anchor.get("status") != "measured"
                or service is None
            ):
                raise ValueError("historical grading needs an actual verified workspace checkpoint")
            actual = service.verify_checkpoint(run_id, anchor)
            snapshot = root / anchor["path"]

        def file(name):
            if not isinstance(name, str) or not name.startswith("work-snapshot/"):
                raise ValueError("grader inputs must belong to the sealed workspace snapshot")
            path = snapshot / name.removeprefix("work-snapshot/")
            if path.resolve() != path or not path.is_relative_to(snapshot):
                raise ValueError("grader input cannot follow links or escape the sealed snapshot")
            if path.is_file() and path.stat().st_size > 8_000_000:
                raise ValueError("grader input exceeds the registered bounded file size")
            return path

        def pointer(value, path):
            if not isinstance(path, str) or (path and not path.startswith("/")):
                raise ValueError("JSON pointer must be empty or begin with a slash")
            for key in path.split("/")[1:]:
                key = key.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
            return value

        checklist, observations = protocol.get("checklist", []), {}
        for item in checklist:
            check = item.get("check", {})
            kind = check.get("kind")
            path = file(check.get("path"))
            observation = {
                "observation_ref": anchor["hash"],
                "snapshot_root_hash": anchor["root_hash"],
                "path": check["path"],
                "check_kind": kind,
            }
            if kind == "file_exists":
                passed = path.is_file()
            elif kind == "sha256_matches":
                expected = check.get("expected")
                if not isinstance(expected, str) or not expected.startswith("sha256:"):
                    raise ValueError("hash checks require a frozen SHA256 expectation")
                entry = actual["entries"].get(str(path.relative_to(snapshot)), {})
                passed = entry.get("sha256") == expected
            elif kind == "json_equals":
                if "expected" not in check:
                    raise ValueError("JSON checks require a frozen expected value")
                try:
                    passed = (
                        pointer(json.loads(path.read_bytes()), check.get("pointer", ""))
                        == check["expected"]
                    )
                except (FileNotFoundError, ValueError, TypeError, KeyError, IndexError):
                    passed = False
            elif kind == "citation_quotes":
                claims = json.loads(path.read_bytes()) if path.is_file() else []
                quotes = verify_citations(claims, archive)
                observation["quote_check"] = quotes
                passed = bool(claims) and all(
                    row["status"] == "quote_present" for row in quotes["items"]
                )
            else:
                raise ValueError("only registered built-in check kinds can execute in this grader")
            observations[item["item_id"]] = {**observation, "passed": passed}
        core = (
            score_checklist(checklist, observations, core_threshold=protocol["core_threshold"])
            if checklist
            else unmeasured("no frozen programmatic checklist")
        )
        hidden = unmeasured("no frozen hidden answer key")
        if protocol.get("hidden_items"):
            path = file(protocol["answers_path"])
            answers = json.loads(path.read_bytes()) if path.is_file() else {}
            if not isinstance(answers, dict):
                raise TypeError("delivered answers must map frozen item IDs to answers")
            hidden = {
                **score_answers(protocol["hidden_items"], answers),
                "observation_ref": anchor["hash"],
            }
        claims = unmeasured("claims validator was not selected by the frozen protocol")
        if protocol.get("claims_validator"):
            path = file(protocol.get("claims_path", "work-snapshot/out/claims.json"))
            claims = await validate_claims(
                json.loads(path.read_bytes()) if path.is_file() else [], self.checkers
            )
        return {
            "status": "measured",
            "column": "V",
            "protocol_id": protocol["protocol_id"],
            "protocol": provenance,
            "goal_metadata": goal_metadata
            or unmeasured("no complete frozen goal metadata was supplied"),
            "preregistered": registered,
            "checklist": core,
            "check_observations": observations,
            "hidden_items": hidden,
            "claims": claims,
            "workspace_snapshot_ref": anchor["hash"],
            "checkpoint": {
                key: anchor[key]
                for key in ("fraction", "usd", "world_actions", "trigger_record_seq")
                if key in anchor
            },
            "scientific_gate_passed": False,
        }
