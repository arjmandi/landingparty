"""Freeze actual private pilot task materials before the first experimental run."""

import hashlib
import json
import os


def _digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _load(root, descriptor):
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("path"), str):
        raise TypeError("Frozen pilot inputs require private path/hash descriptors")
    path = root / descriptor["path"]
    if (
        path.resolve() != path
        or not path.is_relative_to(root / "control" / "study-inputs")
        or not path.is_file()
    ):
        raise ValueError("Pilot source inputs must be nonsymlink control/study-inputs files")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Pilot input exceeds its bounded JSON size")
    body = path.read_bytes()
    if _digest(body) != descriptor.get("hash"):
        raise ValueError("Frozen pilot input hash mismatch")
    return body, json.loads(body)


def _items(value, key, count):
    rows = value.get(key) if isinstance(value, dict) else value
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError(f"The pilot needs exactly {count} frozen {key}")
    normalized = []
    for row in rows:
        if key == "questions":
            text = row.get("question") if isinstance(row, dict) else row
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Each frozen pilot question must contain its actual text")
            normalized.append(" ".join(text.casefold().split()))
        else:
            if not isinstance(row, (str, dict)) or not row:
                raise ValueError("Each frozen trap needs a nonempty registered criterion")
            criterion = (
                {key: value for key, value in row.items() if key not in {"id", "item_id"}}
                if isinstance(row, dict)
                else row.strip()
            )
            if not criterion:
                raise ValueError("A trap identifier cannot substitute for its criterion")
            normalized.append(json.dumps(criterion, sort_keys=True))
    if len(set(normalized)) != count:
        raise ValueError("Frozen pilot questions and traps must be distinct")
    return rows


def freeze_protocol(store, study_id, descriptor, prereg, cells):
    root = store.data_dir
    body, protocol = _load(root, descriptor)
    if not isinstance(protocol, dict):
        raise TypeError("The frozen goal protocol must be an object")
    materials = {"protocol": body}
    for name, count in (("questions", 25), ("traps", 8)):
        raw, value = _load(root, protocol.get(name))
        rows = _items(value, name, count)
        if prereg.get("frozen_" + name + "_hash") != _digest(raw):
            raise ValueError("Pre-registration does not pin the actual frozen " + name)
        materials[name] = raw
        if name == "questions":
            questions = [row["question"] if isinstance(row, dict) else row for row in rows]
    expected = protocol.get("goal_text_hash")
    if not isinstance(expected, str) or prereg.get("frozen_goal_hash") != expected:
        raise ValueError("Pre-registration does not pin the actual rendered goal text")
    for cell in cells:
        goal = cell["config"].get("goal")
        text = goal.get("text") if isinstance(goal, dict) else goal
        if not isinstance(text, str) or _digest(text.encode()) != expected:
            raise ValueError("Every pilot cell must use the actual frozen goal text")
        if any(question not in text for question in questions):
            raise ValueError("The agent goal must contain the twenty-five frozen questions")
        if cell["config"].get("research_mode") == "research":
            from analysis.goals import validate_goal_protocol

            if prereg.get("goal_protocol_hash") != _digest(body):
                raise ValueError(
                    "Research pre-registration must pin the full goal protocol and metadata"
                )
            metadata = validate_goal_protocol(text, protocol.get("metadata", {}))
            if not metadata["valid"]:
                raise ValueError(
                    "The research goal metadata is incomplete: " + "; ".join(metadata["errors"])
                )
    folder = root / "studies" / study_id / "inputs"
    if folder.resolve() != folder:
        raise ValueError("Study inputs cannot use symbolic link directories")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {"goal_text_hash": expected}
    for name, raw in materials.items():
        path = folder / (name + ".json")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        result[name] = {"path": str(path.relative_to(root)), "hash": _digest(raw)}
    return result


def verify_protocol(store, study):
    frozen = study.get("goal_protocol")
    if not frozen:
        return
    folder = store.data_dir / "studies" / study["study_id"] / "inputs"
    for name in ("protocol", "questions", "traps"):
        descriptor = frozen[name]
        path = store.data_dir / descriptor["path"]
        if (
            not path.is_relative_to(folder)
            or path.resolve() != path
            or not path.is_file()
            or _digest(path.read_bytes()) != descriptor["hash"]
        ):
            raise ValueError("A frozen pilot input changed or disappeared before execution")
