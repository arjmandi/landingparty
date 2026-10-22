"""Resolve registered episode references from actual sealed executions."""

import json

from .storage import canonical_json, content_hash

SLOTS = {"workspace", "beliefs", "archive", "context"}


class DependencyPending(ValueError):
    """The registered source has not reached its actual observation yet."""


def validate_references(references, *, external=False):
    if not isinstance(references, dict) or not references or set(references) - SLOTS:
        raise ValueError("Dependencies must name workspace, beliefs, archive, or context sources")
    result = {}
    for slot, original in references.items():
        if not isinstance(original, dict):
            raise ValueError("A dependency must be a structured execution reference")  # noqa: TRY004 - schema validation
        if "runner_id" in original:
            item = {"seed": "same", "session": "last", **original}
            if (
                set(item) - {"runner_id", "session", "seed", "mount"}
                or not isinstance(item["runner_id"], str)
                or not item["runner_id"]
                or not (
                    item["session"] == "last"
                    or isinstance(item["session"], int)
                    and not isinstance(item["session"], bool)
                    and item["session"] >= 1
                )
                or not (
                    item["seed"] == "same"
                    or isinstance(item["seed"], int)
                    and not isinstance(item["seed"], bool)
                )
            ):
                raise ValueError(
                    "A donor life reference requires its actual runner, session, and seed"
                )
            if slot == "beliefs":
                item.setdefault("mount", "pattern")
                if item["mount"] not in {"pattern", "continuity"}:
                    raise ValueError("Belief dependencies use pattern or continuity mounting")
            elif "mount" in item:
                raise ValueError("Only a belief dependency has a mounting policy")
            result[slot] = item
            continue
        item = {"seed": "same", "episode": 1, **original}
        if set(item) - {"source_cell", "seed", "episode", "study_id", "mount"}:
            raise ValueError("Unknown dependency reference field")
        if (
            not isinstance(item.get("source_cell"), str)
            or not item["source_cell"]
            or len(item["source_cell"]) > 100
            or isinstance(item["episode"], bool)
            or not isinstance(item["episode"], int)
            or not 1 <= item["episode"] <= 100
            or not (
                item["seed"] == "same"
                or isinstance(item["seed"], int)
                and not isinstance(item["seed"], bool)
            )
            or external
            and not item.get("study_id")
            or "study_id" in item
            and not isinstance(item["study_id"], str)
        ):
            raise ValueError("Dependencies require an actual cell, episode, and declared seed")
        if slot == "beliefs":
            item.setdefault("mount", "pattern")
            if item["mount"] not in {"pattern", "continuity"}:
                raise ValueError("Belief dependencies use pattern or continuity mounting")
        elif "mount" in item:
            raise ValueError("Only a belief dependency has a mounting policy")
        result[slot] = item
    return json.loads(canonical_json(result))


def dependency_protocol(cells, prereg):
    """Freeze a topological graph; no future identifiers or string substitution."""
    if not any(cell.get("depends_on") for cell in cells):
        return None
    previous, protocol = {}, []
    for cell in cells:
        episode = cell.get("episode", 1)
        if isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 100:
            raise ValueError("An episode must be an integer between one and 100")
        references = validate_references(cell["depends_on"]) if cell.get("depends_on") else {}
        for reference in references.values():
            if reference.get("study_id") or reference.get("runner_id"):
                continue
            source = previous.get(reference["source_cell"])
            if source is None or (
                not source["config"].get("life_id")
                and source.get("episode", 1) != reference["episode"]
            ):
                raise ValueError("A dependency must reference an earlier declared episode")
            required = cell["seeds"] if reference["seed"] == "same" else [reference["seed"]]
            if not set(required).issubset(source["seeds"]):
                raise ValueError("The source episode does not declare every referenced seed")
        cell.update(episode=episode, depends_on=references)
        protocol.append(
            {
                "cell_id": cell["cell_id"],
                "condition_id": cell.get("condition_id", cell["cell_id"]),
                "episode": episode,
                "depends_on": references,
                "conditions": {
                    "config": {
                        key: value for key, value in cell["config"].items() if key != "series_id"
                    },
                    **{
                        key: cell.get(key)
                        for key in (
                            "seeds",
                            "baseline",
                            "model",
                            "max_steps",
                            "max_tokens",
                            "revision_turns",
                            "seed_urls",
                            "queries",
                            "recipients",
                            "analysis_options",
                        )
                    },
                },
            }
        )
        previous[cell["cell_id"]] = cell
    result = {"version": "study-dependencies@1", "cells": protocol}
    fingerprint = content_hash(result)
    if (
        any(cell["config"]["research_mode"] == "research" for cell in cells)
        and prereg.get("dependency_protocol_hash") != fingerprint
    ):
        raise ValueError(
            "Research dependencies must match the frozen pre-registration protocol hash"
        )
    return {"hash": fingerprint, "protocol": result}


def execution_schedule(cells, schedules):
    """Stable topological order of actual cell/seed nodes, preferring interleaving."""
    remaining = [
        (cell, schedule[index])
        for index in range(max(map(len, schedules)))
        for cell, schedule in zip(cells, schedules, strict=True)
        if index < len(schedule)
    ]
    complete, ordered = set(), []
    while remaining:
        for index, (cell, seed) in enumerate(remaining):
            dependencies = {
                (
                    reference["source_cell"],
                    seed if reference["seed"] == "same" else reference["seed"],
                )
                for reference in cell.get("depends_on", {}).values()
                if not reference.get("study_id") and not reference.get("runner_id")
            }
            if dependencies <= complete:
                ordered.append((cell, seed))
                complete.add((cell["cell_id"], seed))
                remaining.pop(index)
                break
        else:
            raise ValueError("The registered execution dependency graph has no available source")
    return ordered


def _captured_context(service, run):
    root = service.store.run_dir(run["run_id"])
    life_id = run["config"].get("life_id")
    if life_id:
        lives = service.world.services["lives"]
        for event in reversed(lives.events(life_id)):
            if event["kind"] != "harness_context" or event.get("run_id") != run["run_id"]:
                continue
            reference = event["context"]
            path = root / reference["path"]
            if (
                path.resolve() != path
                or not path.is_relative_to(root)
                or not lives.verify(life_id)["valid"]
            ):
                raise ValueError("The donor's captured context failed life integrity")
            messages = json.loads(path.read_bytes())
            if not isinstance(messages, list) or content_hash(messages) != reference["hash"]:
                raise ValueError("The donor's captured context changed")
            return messages, reference
    from analysis.instruments import load_artifact

    for event in reversed(service.store.records(run["run_id"])):
        if event["kind"] != "model_capture":
            continue
        body, reference = load_artifact(
            service.store,
            run["run_id"],
            {
                "path": event["capture_ref"],
                "hash": event["capture_hash"],
            },
        )
        capture = json.loads(body)
        response = capture.get("response")
        response = json.loads(response) if isinstance(response, str) else response
        messages = capture.get("request", {}).get("messages")
        if (
            not isinstance(messages, list)
            or not isinstance(response, dict)
            or not response.get("choices")
        ):
            raise ValueError(
                "The actual provider context cannot be resumed by the reference adapter"
            )
        message = response["choices"][0].get("message")
        if not isinstance(message, dict) or message.get("tool_calls"):
            raise ValueError("The captured donor context has unresolved native tool calls")
        return [*messages, message], {"run_id": run["run_id"], **reference}
    raise ValueError("The donor has no actual captured context")


def resolve_dependencies(service, references, seed, *, study_id=None):
    """Read actual registered source rows and return exact config substitutions."""
    references = validate_references(references, external=study_id is None)
    registry = service.world.services.get("registry")
    if registry is None:
        raise ValueError("Dependency execution requires the actual series registry")
    receipts, iota, archive, continuity, context = {}, {}, None, None, None
    for slot, reference in references.items():
        source_study_id = reference.get("study_id", study_id)
        selected_seed = seed if reference["seed"] == "same" else reference["seed"]
        if reference.get("runner_id"):
            runner = service.get_life_runner(reference["runner_id"])
            life = service.world.services["lives"].get(runner["life_id"])
            if runner["state"] != "completed" or not life["ended"] or not life["sessions"]:
                if runner["state"] in {"planned", "running", "waiting", "paused"}:
                    raise DependencyPending("The actual donor life is not yet complete")
                raise ValueError("The actual donor life has not completed")
            index = -1 if reference["session"] == "last" else reference["session"] - 1
            if index >= len(life["sessions"]):
                raise ValueError("The donor life did not produce the requested session")
            run_id = life["sessions"][index]["run_id"]
            source_study_id = None
        else:
            study = service.get(source_study_id)
            plans = [cell for cell in study["cells"] if cell["cell_id"] == reference["source_cell"]]
            if len(plans) != 1 or (
                not plans[0]["config"].get("life_id")
                and plans[0].get("episode", 1) != reference["episode"]
            ):
                raise ValueError("The dependency source episode is not registered")
            rows = [
                row
                for row in registry.execution_log(source_study_id)
                if row["cell"] == reference["source_cell"]
                and row["seed"] == selected_seed
                and row.get("episode", 1) == reference["episode"]
            ]
            if len(rows) != 1:
                if not rows and study.get("execution_kind") == "lives":
                    joins = [
                        join
                        for join in study.get("life_runners", [])
                        if join["cell_id"] == reference["source_cell"]
                        and join["seed"] == selected_seed
                    ]
                    if joins:
                        source_runner = service.get_life_runner(joins[0]["runner_id"])
                        source_life = service.world.services["lives"].get(joins[0]["life_id"])
                        if source_runner["state"] in {"failed", "paused"} or source_life["ended"]:
                            raise ValueError(
                                "The actual source life ended before the registered dependency episode"
                            )
                if not rows and study["state"] in {"planned", "running"}:
                    raise DependencyPending("The registered source episode has not executed yet")
                raise ValueError(
                    "The dependency source has not produced exactly one actual execution"
                )
            run_id = rows[0]["run_id"]
        run = service.store.get_run(run_id)
        records = service.store.records(run_id)
        if run["state"] in {"running", "frozen", "preflight", "stopping"}:
            raise DependencyPending("The actual source session has not sealed yet")
        if (
            run["state"] != "sealed"
            or run.get("tainted")
            or not records
            or records[-1]["kind"] != "seal"
            or not service.store.verify_record(run_id)["valid"]
            or not service.store.reconcile(run_id)["valid"]
            or run.get("end", {}).get("truncated")
            or run["config"].get("seed") != selected_seed
        ):
            raise ValueError("Dependency source is failed, unsealed, tainted, or incomplete")
        series = registry.get_series(run["config"]["series_id"])
        registry.validate_run(series["series_id"], run["config"])
        if series["created_at"] > run["created_at"]:
            raise ValueError("The actual donor's registration postdates its source")
        receipts[slot] = {
            "reference": reference,
            "source_study_id": source_study_id,
            "source_run_id": run_id,
            "seed": selected_seed,
            "record_hash": records[-1]["hash"],
            "series_id": series["series_id"],
            "prereg_hash": series["prereg_hash"],
            "source_runner_id": reference.get("runner_id"),
            "model_snapshot_id": run["config"].get("model_snapshot_id"),
        }
        if slot == "archive":
            archive = run_id
        elif slot == "context":
            messages, context_reference = _captured_context(service, run)
            context = {"initial_messages": messages, "context_refs": [context_reference]}
            receipts[slot]["context_ref"] = context_reference
        else:
            iota[slot] = f"import({run_id})"
            if slot == "beliefs" and reference["mount"] == "continuity":
                continuity = run_id
    changes = {"iota": iota}
    if archive:
        changes.update(mode="replay", archive_in=archive)
    if continuity:
        changes["continuity_source_run_id"] = continuity
    if context:
        changes["captured_context"] = context
        changes["context_import_policy"] = "captured"
    return changes, receipts
