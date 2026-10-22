"""Bind supplied research instrument rows to private, hash-pinned artifacts.

This verifies provenance references and artifact integrity, not the scientific
validity of a researcher's scoring procedure. It cannot unlock study gates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_artifact(store, source_run_id, descriptor, *, allowed_run_ids=()):
    if not isinstance(descriptor, dict):
        raise TypeError("instrument artifacts require path and hash descriptors")
    run_id = descriptor.get("run_id", source_run_id)
    run = store.get_run(run_id)
    if (
        run_id != source_run_id
        and run_id not in allowed_run_ids
        and run["config"].get("measurement_of") != source_run_id
    ):
        raise ValueError("instrument references must belong to the source or its measurement runs")
    if (
        run["state"] not in {"sealed", "ended", "killed"}
        or run.get("tainted")
        or store.verify_record(run_id).get("valid") is not True
    ):
        raise ValueError("instrument artifacts require sealed, untainted verified records")
    root = store.run_dir(run_id).resolve()
    name = descriptor.get("path")
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("instrument artifact paths must be relative to their private run")
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file() or (root / name).is_symlink():
        raise ValueError("instrument artifact paths must stay inside their private run")
    body = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if descriptor.get("hash") != digest:
        raise ValueError("instrument artifact hash mismatch")
    return body, {"run_id": run_id, "path": name, "hash": digest}


def _references(value):
    if isinstance(value, list):
        for item in value:
            yield from _references(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "observation_ref",
                "context_ref",
                "receipt_ref",
                "early_ref",
                "late_ref",
                "prediction_ref",
                "outcome_ref",
            }:
                if not isinstance(item, str) or not item:
                    raise ValueError("measurement references must be nonempty strings")
                yield item
            elif key == "observation_refs":
                if not isinstance(item, list) or not all(
                    isinstance(ref, str) and ref for ref in item
                ):
                    raise ValueError("observation_refs must be a list of reference strings")
                yield from item
            else:
                yield from _references(item)


def load_instrument(
    store, source_run_id, descriptor, *, allowed_run_ids=(), reference_ids=(), require_rows=True
):
    """Load ``{rows, reference_artifacts?, baseline_rows?}`` from a pinned file.

    Reference strings must identify a source record (hash, event/action/call id,
    or ``record:<seq>``), or a named, independently hash-verified private file in
    reference_artifacts. Child measurement artifacts must link to this source.
    Raw caller-supplied rows and aggregate baseline scores are not accepted.
    """
    body, artifact = load_artifact(
        store, source_run_id, descriptor, allowed_run_ids=allowed_run_ids
    )
    payload = json.loads(body)
    if not isinstance(payload, dict) or (
        require_rows and not isinstance(payload.get("rows"), list)
    ):
        raise TypeError("instrument payload must contain a rows array")
    if "baselines" in payload:
        raise ValueError("supply measured baseline_rows instead of aggregate baseline claims")
    known = set(reference_ids)
    for run_id in {source_run_id, *allowed_run_ids}:
        run = store.get_run(run_id)
        if run["state"] not in {"sealed", "ended", "killed"}:
            continue  # A live session cannot supply post-seal measurement evidence.
        if run.get("tainted") or store.verify_record(run_id).get("valid") is not True:
            raise ValueError("instrument source records must be verified and untainted")
        for record in store.records(run_id):
            known.add(f"{run_id}:record:{record['seq']}")
            if run_id == source_run_id:
                known.add(f"record:{record['seq']}")
            for key in ("hash", "event_id", "action_id", "call_id"):
                if isinstance(record.get(key), str):
                    known.add(record[key])
            event = record.get("event")
            if isinstance(event, dict) and isinstance(event.get("event_id"), str):
                known.add(event["event_id"])
    verified = {}
    references = payload.get("reference_artifacts", {})
    if not isinstance(references, dict):
        raise TypeError("reference_artifacts must map reference names to pinned files")
    for name, reference in references.items():
        if not isinstance(name, str) or not name or name in known:
            raise ValueError("artifact reference names must be distinct from record identities")
        _, verified[name] = load_artifact(
            store, source_run_id, reference, allowed_run_ids=allowed_run_ids
        )
        known.add(name)
    baseline_rows = payload.get("baseline_rows", {})
    if not isinstance(baseline_rows, dict) or any(
        name not in {"B3", "B3b"} or not isinstance(rows, list)
        for name, rows in baseline_rows.items()
    ):
        raise ValueError("baseline_rows must map B3/B3b to actual measurement arrays")
    for reference in _references(
        {key: value for key, value in payload.items() if key != "reference_artifacts"}
    ):
        if reference not in known:
            raise ValueError("instrument contains an unverified measurement reference")
    return {
        "rows": payload.get("rows", []),
        "payload": payload,
        "baseline_rows": baseline_rows,
        "provenance": {
            "artifact": artifact,
            "reference_artifacts": verified,
            "reference_integrity_verified": True,
            "measurement_validity": "requires the registered instrument and calibration review",
            "scientific_gate_passed": False,
        },
    }
