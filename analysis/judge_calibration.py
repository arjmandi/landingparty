"""Run expert-labelled calibration through the same actual metered judge prompts."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .judges import ToollessJudge
from .metrics import calibration
from .values import _receipt

TASKS = {"citation_support", "goal_recovery", "ask_classification"}


async def calibrate_judge(
    world,
    model,
    source_run_id,
    spec,
    *,
    agent_family,
    rows,
    artifact_hash,
    budget_usd,
    max_tokens=2048,
    minimum_items=40,
):
    if not isinstance(rows, list) or not rows:
        raise ValueError("judge calibration needs a nonempty frozen expert-labelled sample")
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("task") not in TASKS
            or not isinstance(row.get("input"), dict)
            or "label" not in row
        ):
            raise ValueError("calibration rows need task, input and expert label")
        label = row["label"]
        if row["task"] == "citation_support" and not isinstance(label, bool):
            raise ValueError("citation expert labels must be boolean")
        if row["task"] == "ask_classification" and label not in {
            "blocker",
            "approval",
            "diagnostic",
            "choice",
        }:
            raise ValueError("ask expert labels must name an actual ask class")
        if row["task"] == "goal_recovery" and label is not None and not isinstance(label, str):
            raise ValueError("goal expert labels must be canonical goal strings or null")
    cap_nano = int(Decimal(str(budget_usd)) * 10**9) // len(rows)
    if cap_nano < 1:
        raise ValueError("calibration budget cannot reserve one nanodollar per item")
    judge = ToollessJudge(
        model,
        replace(spec, human_agreement=None),
        agent_family=agent_family,
        budget_usd=cap_nano / 1e9,
        max_tokens=max_tokens,
    )
    results, seen = [], {run["run_id"] for run in world.store.list_runs()}
    for row in rows:
        task, data = row["task"], row["input"]
        if task == "citation_support":
            result = await judge.citation_support(
                source_run_id, data["claim"], data["quote"], data["source"]
            )
            predicted = result["judgment"]["supported"]
        elif task == "goal_recovery":
            result = await judge.label_goal(source_run_id, data["actions"])
            predicted = result["judgment"]["goal"]
            if predicted is not None and not isinstance(predicted, str):
                raise ValueError("goal calibration returned an invalid canonical label")
        else:
            result = await judge.classify_ask(source_run_id, data["ask"])
            predicted = result["judgment"]["ask_class"]
        _receipt(world.store, source_run_id, spec, result, seen=seen)
        results.append(
            {"task": task, "expert_label": row["label"], "predicted": predicted, "reading": result}
        )
    summaries = {}
    for task in sorted({row["task"] for row in rows}):
        samples = [row for row in results if row["task"] == task]
        labels, predictions = (
            [row["expert_label"] for row in samples],
            [row["predicted"] for row in samples],
        )
        summary = calibration(labels, predictions)
        summary["per_class"] = [
            {"label": label, **calibration(labels, predictions, positive=label)}
            for label in sorted(set(labels) | set(predictions), key=str)
        ]
        summary.update(
            judge_id=spec.model,
            prompt_id=spec.prompt_id,
            calibration_set_hash=artifact_hash,
            receipts_verified=True,
            minimum_items=minimum_items,
        )
        if len(samples) < minimum_items:
            summary.update(
                column="J",
                exploratory=True,
                reason="fewer than the registered forty expert-labelled items",
            )
        summaries[task] = summary
    return {
        "tasks": summaries,
        "readings": results,
        "calibration_set_hash": artifact_hash,
        "receipts_verified": True,
        "scientific_gate_passed": False,
    }
