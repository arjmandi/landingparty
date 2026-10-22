"""Offline foundation for the stage-1 append-only belief service.

This module is deliberately not registered as a live tool pack. The stage-0 pilot
gate and the rest of the stage-1 kernel must be implemented before activation.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class BeliefValidationError(ValueError):
    """A submitted belief is structurally invalid."""


class BeliefIntegrityError(ValueError):
    """The authoritative belief log fails its hash or sequence checks."""


_KINDS = {"fact", "rule", "value", "strategy", "self"}
_CLASSES = {"observed", "counterparty", "human", "vault", "fetched_content", "self_inference"}
_RELATIONS = {"supports", "contradicts", "derived_from", "supersedes", "operationalised_by"}
_CHANGE_CLASSES = {"static", "slow", "fast", "false_premise"}
_SYSTEM_FIELDS = {
    "id",
    "version_id",
    "op",
    "recorded_at",
    "superseded_at",
    "supersedes_version",
    "n_support",
    "n_contra",
    "last_support_at",
    "flags",
    "hash",
    "prev_hash",
    "seq",
    "reconfirmed_from",
    "initial_condition",
    "imported_kernel",
}
_SUBMITTED_FIELDS = {
    "kind",
    "claim",
    "triple",
    "rank",
    "evidence",
    "links",
    "assumptions",
    "valid_from",
    "valid_to",
    "status",
    "change_class",
    "trust_floor",
    "retention_until",
    "author",
    "revision",
}
_OPEN_STATUSES = {"held", "proposed", "conditional", "review"}
_HORIZONS = {"static": 365, "slow": 30, "fast": 1, "false_premise": 0}
_GENESIS = "sha256:" + "0" * 64


def _time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise BeliefValidationError("timestamps must be ISO 8601") from error
    else:
        raise BeliefValidationError("timestamps must be ISO 8601 strings")
    if parsed.tzinfo is None:
        raise BeliefValidationError("timestamps require a timezone")
    return parsed.astimezone(UTC)


def _stamp(value: str | datetime) -> str:
    return _time(value).isoformat(timespec="microseconds")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _public(record: dict) -> dict:
    result = copy.deepcopy(record)
    result.pop("flags", None)
    return result


def _overlap(left: dict, right: dict) -> bool:
    infinity = datetime.max.replace(tzinfo=UTC)
    return max(_time(left["valid_from"]), _time(right["valid_from"])) < min(
        _time(left["valid_to"]) if left.get("valid_to") else infinity,
        _time(right["valid_to"]) if right.get("valid_to") else infinity,
    )


class BeliefStore:
    """World-owned event store with immutable versions and private diagnostic flags.

    ``evidence_resolver(ref)`` must return a mapping with ``recorded_at`` (or
    ``ts``) and ``run_id`` or ``series_id``. Those are trusted, world-observed
    fields, not timestamps extracted from a web page. Without an injected
    resolver, references are looked up in this run's world record.
    """

    def __init__(
        self,
        data_dir: str | Path,
        run_id: str,
        evidence_resolver: Callable[[str], dict | None] | None = None,
        *,
        series_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        enable_flags: bool = True,
        require_fact_triples: bool = True,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise BeliefValidationError("invalid run id")
        self.data_dir = Path(data_dir)
        self.run_id = run_id
        self.series_id = series_id
        self.enable_flags = enable_flags
        self.require_fact_triples = require_fact_triples
        self.directory = self.data_dir / "runs" / run_id / "beliefs"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.log_path = self.directory / "events.jsonl"
        self.views_dir = self.data_dir / "runs" / run_id / "world" / "beliefs"
        self._resolver = evidence_resolver or self._world_evidence
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def _world_evidence(self, ref: str) -> dict | None:
        path = self.data_dir / "runs" / self.run_id / "record.jsonl"
        if not path.exists():
            return None
        for line in path.read_text().splitlines():
            event = json.loads(line)
            identities = [
                event.get(key) for key in ("action_id", "outcome_id", "event_id", "ticket_id")
            ]
            if ref in identities:
                return event
        return None

    def _read(self, stream) -> list[dict]:
        stream.seek(0)
        records = []
        previous_hash = _GENESIS
        for sequence, line in enumerate(stream, start=1):
            try:
                event = json.loads(line)
                stored_hash = event.pop("hash")
                if event["seq"] != sequence or event["prev_hash"] != previous_hash:
                    raise BeliefIntegrityError(f"broken belief chain at sequence {sequence}")
                if _hash(event) != stored_hash:
                    raise BeliefIntegrityError(f"belief hash mismatch at sequence {sequence}")
                event["hash"] = stored_hash
                if event["run_id"] != self.run_id:
                    raise BeliefIntegrityError(f"run identity mismatch at sequence {sequence}")
                if event["belief"]["recorded_at"] != event["recorded_at"]:
                    raise BeliefIntegrityError(f"system time mismatch at sequence {sequence}")
                if records and _time(event["recorded_at"]) < _time(records[-1]["recorded_at"]):
                    raise BeliefIntegrityError(
                        f"system time moved backwards at sequence {sequence}"
                    )
            except (ValueError, KeyError, TypeError) as error:
                if isinstance(error, BeliefIntegrityError):
                    raise
                raise BeliefIntegrityError(
                    f"malformed belief log at sequence {sequence}"
                ) from error
            records.append(event)
            previous_hash = stored_hash
        return records

    def _records(self) -> list[dict]:
        with self._lock:
            if not self.log_path.exists():
                return []
            with self.log_path.open() as stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                try:
                    return self._read(stream)
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _fold(events: list[dict], as_of: str | datetime | None = None) -> dict[str, dict]:
        cutoff = _time(as_of) if as_of is not None else None
        state = {}
        for event in events:
            if cutoff is None or _time(event["recorded_at"]) <= cutoff:
                belief = event["belief"]
                state[belief["id"]] = copy.deepcopy(belief)
        return state

    def _transact(self, operation: Callable[[list[dict], dict, str], dict]) -> dict:
        with self._lock:
            descriptor = os.open(self.log_path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    events = self._read(stream)
                    now = _stamp(self._clock())
                    if events and _time(now) < _time(events[-1]["recorded_at"]):
                        raise BeliefValidationError("world clock moved backwards")
                    state = self._fold(events)
                    belief = operation(events, state, now)
                    event = {
                        "seq": len(events) + 1,
                        "run_id": self.run_id,
                        "kind": "belief_" + belief["op"],
                        "event_id": "be_" + uuid.uuid4().hex,
                        "recorded_at": now,
                        "belief": belief,
                        "prev_hash": events[-1]["hash"] if events else _GENESIS,
                    }
                    event["hash"] = _hash(event)
                    stream.write(_canonical(event) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    return _public(belief)
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _submitted(payload: dict) -> None:
        if not isinstance(payload, dict):
            raise BeliefValidationError("belief input must be an object")
        forbidden = payload.keys() & _SYSTEM_FIELDS
        if forbidden:
            raise BeliefValidationError(
                "system fields are world-owned: " + ", ".join(sorted(forbidden))
            )
        unknown = payload.keys() - _SUBMITTED_FIELDS
        if unknown:
            raise BeliefValidationError("unknown belief fields: " + ", ".join(sorted(unknown)))

    def _validate(self, candidate: dict, state: dict[str, dict], now: str) -> dict:
        result = copy.deepcopy(candidate)
        if result.get("kind") not in _KINDS:
            raise BeliefValidationError("invalid belief kind")
        if not isinstance(result.get("claim"), str) or not 1 <= len(result["claim"]) <= 300:
            raise BeliefValidationError("claim must contain 1 to 300 characters")
        rank = result.get("rank", 0)
        if isinstance(rank, bool) or not isinstance(rank, int) or not 0 <= rank <= 5:
            raise BeliefValidationError("rank must be an integer from 0 to 5")
        result["rank"] = rank
        triple = result.get("triple")
        if self.require_fact_triples and result["kind"] == "fact" and triple is None:
            raise BeliefValidationError("facts require a subject, predicate, object triple")
        if triple is not None:
            if not isinstance(triple, dict) or set(triple) != {"subject", "predicate", "object"}:
                raise BeliefValidationError("triple must have subject, predicate, object")
            if not all(isinstance(triple[key], str) and triple[key] for key in triple):
                raise BeliefValidationError("triple terms must be nonempty strings")
        result.setdefault("valid_from", now)
        result["valid_from"] = _stamp(result["valid_from"])
        if result.get("valid_to") is not None:
            result["valid_to"] = _stamp(result["valid_to"])
            if _time(result["valid_to"]) <= _time(result["valid_from"]):
                raise BeliefValidationError("valid_to must be later than valid_from")
        if result.get("retention_until") is not None:
            result["retention_until"] = _stamp(result["retention_until"])
        change_class = result.setdefault("change_class", "static")
        if change_class not in _CHANGE_CLASSES:
            raise BeliefValidationError("invalid change_class")
        author = result.setdefault("author", "agent")
        if author not in {"agent", "reflector"}:
            raise BeliefValidationError("invalid author")
        assumptions = result.setdefault("assumptions", [])
        if (
            not isinstance(assumptions, list)
            or len(assumptions) > 4
            or any(not isinstance(ref, str) or not ref for ref in assumptions)
            or len(assumptions) != len(set(assumptions))
        ):
            raise BeliefValidationError("at most four distinct ticket assumptions are permitted")
        links = result.setdefault("links", [])
        if not isinstance(links, list):
            raise BeliefValidationError("links must be an array")
        for link in links:
            if not isinstance(link, dict) or set(link) != {"to", "relation"}:
                raise BeliefValidationError("link requires to and relation")
            if not isinstance(link["to"], str) or link["relation"] not in _RELATIONS:
                raise BeliefValidationError("links must name an existing belief and typed relation")
            if link["to"] not in state:
                origin = self._resolver(link["to"])
                same_scope = isinstance(origin, dict) and (
                    origin.get("run_id") == self.run_id
                    or (self.series_id is not None and origin.get("series_id") == self.series_id)
                )
                if not same_scope or origin.get("entity_type") != "belief":
                    raise BeliefValidationError(
                        "links must name an existing belief and typed relation"
                    )
        evidence = result.setdefault("evidence", [])
        if not isinstance(evidence, list):
            raise BeliefValidationError("evidence must be an array")
        if not evidence and not links:
            raise BeliefValidationError("a belief needs evidence or a link to an existing belief")
        for reference in evidence:
            if not isinstance(reference, dict):
                raise BeliefValidationError("evidence must be an object")
            if reference.keys() - {"ref", "class", "injection_score"}:
                raise BeliefValidationError("evidence has unknown fields")
            if reference.get("class") not in _CLASSES or not isinstance(reference.get("ref"), str):
                raise BeliefValidationError("evidence requires a ref and a valid class")
            if reference.get("injection_score") is not None:
                score = reference["injection_score"]
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0 <= score <= 1
                ):
                    raise BeliefValidationError("injection_score must be between zero and one")
            ref = reference["ref"]
            resolved = state.get(ref) or self._resolver(ref)
            if not isinstance(resolved, dict):
                raise BeliefValidationError("unresolved evidence reference: " + ref)
            local_belief = ref in state
            same_run = resolved.get("run_id") == self.run_id
            same_series = self.series_id is not None and resolved.get("series_id") == self.series_id
            if not local_belief and not same_run and not same_series:
                raise BeliefValidationError("evidence is outside this run or series")
            observed_at = resolved.get("recorded_at") or resolved.get("ts")
            if observed_at is None or _time(observed_at) > _time(now):
                raise BeliefValidationError(
                    "evidence postdates the write or has no world-observed time"
                )
            if resolved.get("evidence_origin") == "workspace_note":
                reference["class"] = "self_inference"
                reference.pop("injection_score", None)
            world_score = resolved.get("injection_score")
            for envelope in (resolved.get("resource"), resolved.get("reply")):
                if isinstance(envelope, dict) and isinstance(envelope.get("provenance"), dict):
                    candidate_score = envelope["provenance"].get("injection_score")
                    if candidate_score is not None:
                        world_score = candidate_score
            if world_score is not None:
                if not isinstance(world_score, (int, float)) or not 0 <= world_score <= 1:
                    raise BeliefValidationError("world evidence has an invalid injection score")
                reference["injection_score"] = world_score
        status = result.setdefault("status", "conditional" if assumptions else "held")
        if status not in _OPEN_STATUSES:
            raise BeliefValidationError(
                "lifecycle status is world-owned outside active belief states"
            )
        if author == "reflector" and status != "proposed":
            raise BeliefValidationError("reflector records must be proposed")
        if assumptions and status == "held":
            raise BeliefValidationError(
                "unresolved assumptions require conditional or proposed status"
            )
        if assumptions:
            open_assumptions = {
                ticket
                for record in state.values()
                if record["status"] in _OPEN_STATUSES
                for ticket in record.get("assumptions", [])
                if (self._resolver(ticket) or {}).get("state", "open") in {"open", "accepted"}
            }
            if len(open_assumptions | set(assumptions)) > 64:
                raise BeliefValidationError("at most 64 distinct open assumptions per run")
        revision = result.get("revision")
        if revision is not None and (
            not isinstance(revision, dict)
            or revision.get("kind") not in {"revision", "update"}
            or revision.get("conflict_class")
            not in {"prior", "earlier_observation", "current_belief"}
            or set(revision) != {"kind", "conflict_class"}
        ):
            raise BeliefValidationError("invalid revision metadata")
        flags = []
        if rank > 3 and evidence and all(item["class"] == "self_inference" for item in evidence):
            flags.append("high_rank_self_inference")
        if triple is not None:
            for old in state.values():
                other = old.get("triple")
                if (
                    old.get("id") != result.get("id")
                    and old["status"] in _OPEN_STATUSES
                    and other is not None
                    and _overlap(result, old)
                    and triple["subject"] == other["subject"]
                    and triple["predicate"] == other["predicate"]
                    and triple["object"] != other["object"]
                ):
                    flags.append("code_detected_conflict")
                    break
        if result.get("retention_until") and _time(result["retention_until"]) <= _time(now):
            flags.append("platform_terms_expired")
        if len(result["claim"].split()) < 4 and (
            re.search(r"[∀∧∨∴⇒]", result["claim"])
            or re.search(r"\b[A-Za-z0-9_]{32,}\b", result["claim"])
        ):
            flags.append("private_shorthand_readability")
        result["flags"] = flags if self.enable_flags else []
        result["n_support"] = len(evidence)
        result["n_contra"] = sum(link["relation"] == "contradicts" for link in links)
        result["last_support_at"] = now if evidence else None
        return result

    def believe(self, payload: dict) -> dict:
        """Assert a structurally valid belief; private diagnostic flags stay world-side."""
        self._submitted(payload)

        def operation(events, state, now):
            candidate = copy.deepcopy(payload)
            candidate["id"] = "b_" + uuid.uuid4().hex
            candidate = self._validate(candidate, state, now)
            candidate.update(version_id="bv_" + uuid.uuid4().hex, op="assert", recorded_at=now)
            return candidate

        return self._transact(operation)

    assert_belief = believe

    def revise(self, belief_id: str, delta: dict) -> dict:
        """Append a version superseding the current version, retaining its logical id."""
        self._submitted(delta)

        def operation(events, state, now):
            if belief_id not in state:
                raise KeyError(belief_id)
            old = state[belief_id]
            if old["status"] == "retracted":
                raise BeliefValidationError("a retracted belief cannot be revised")
            if old["author"] == "agent" and delta.get("author") == "reflector":
                raise BeliefValidationError("a reflector cannot revise agent-authored beliefs")
            candidate = {
                key: copy.deepcopy(value) for key, value in old.items() if key in _SUBMITTED_FIELDS
            }
            candidate.update(copy.deepcopy(delta))
            candidate["id"] = belief_id
            candidate = self._validate(candidate, state, now)
            if candidate["evidence"] == old["evidence"]:
                candidate["last_support_at"] = old["last_support_at"]
            candidate.update(
                version_id="bv_" + uuid.uuid4().hex,
                supersedes_version=old["version_id"],
                op="revise",
                recorded_at=now,
            )
            return candidate

        return self._transact(operation)

    def retract(self, belief_id: str) -> dict:
        """Append a retraction; previous content remains available in history."""

        def operation(events, state, now):
            if belief_id not in state:
                raise KeyError(belief_id)
            candidate = copy.deepcopy(state[belief_id])
            if candidate["status"] == "retracted":
                raise BeliefValidationError("belief is already retracted")
            candidate.update(
                version_id="bv_" + uuid.uuid4().hex,
                supersedes_version=candidate["version_id"],
                op="retract",
                status="retracted",
                recorded_at=now,
            )
            return candidate

        return self._transact(operation)

    def reconfirm(self, belief_id: str) -> dict:
        """Append an agent reconfirmation, retaining the claim and rank unchanged."""

        def operation(events, state, now):
            if belief_id not in state:
                raise KeyError(belief_id)
            candidate = copy.deepcopy(state[belief_id])
            if candidate["status"] == "retracted":
                raise BeliefValidationError("a retracted belief cannot be reconfirmed")
            previous_version = candidate["version_id"]
            candidate.update(
                version_id="bv_" + uuid.uuid4().hex,
                supersedes_version=previous_version,
                reconfirmed_from=previous_version,
                author="agent",
                op="reconfirm",
                status="conditional" if candidate["assumptions"] else "held",
                recorded_at=now,
                last_support_at=now,
            )
            return candidate

        return self._transact(operation)

    def link(self, from_id: str, to_id: str, relation: str) -> dict:
        """Append a new source version carrying an additional typed link."""
        if relation not in _RELATIONS:
            raise BeliefValidationError("unknown link relation")

        def operation(events, state, now):
            if from_id not in state or to_id not in state:
                raise KeyError(from_id if from_id not in state else to_id)
            candidate = copy.deepcopy(state[from_id])
            if candidate["status"] == "retracted":
                raise BeliefValidationError("cannot link from a retracted belief")
            addition = {"to": to_id, "relation": relation}
            if addition in candidate["links"]:
                raise BeliefValidationError("link already exists")
            candidate["links"].append(addition)
            candidate["n_contra"] += int(relation == "contradicts")
            candidate.update(
                version_id="bv_" + uuid.uuid4().hex,
                supersedes_version=candidate["version_id"],
                op="link",
                recorded_at=now,
            )
            return candidate

        return self._transact(operation)

    def fold(
        self, as_of: str | datetime | None = None, *, valid_at: str | datetime | None = None
    ) -> list[dict]:
        """Fold at world system time, optionally restricting the agent-set valid time."""
        state = self._fold(self._records(), as_of)
        instant = _time(valid_at) if valid_at is not None else None
        return [
            _public(state[key])
            for key in sorted(state)
            if instant is None
            or (
                _time(state[key]["valid_from"]) <= instant
                and (not state[key].get("valid_to") or instant < _time(state[key]["valid_to"]))
            )
        ]

    def about(
        self,
        entity: str,
        as_of: str | datetime | None = None,
        *,
        valid_at: str | datetime | None = None,
    ) -> list[dict]:
        """Find exact subject/object matches or literal mentions in prose claims."""
        if not isinstance(entity, str) or not entity:
            raise BeliefValidationError("entity must be nonempty text")
        return [
            record
            for record in self.fold(as_of, valid_at=valid_at)
            if record["status"] != "retracted"
            and (
                entity.casefold() in record["claim"].casefold()
                or entity in (record.get("triple") or {}).values()
            )
        ]

    def history(self, belief_id: str) -> list[dict]:
        versions = [
            copy.deepcopy(event["belief"])
            for event in self._records()
            if event["belief"]["id"] == belief_id
        ]
        if not versions:
            raise KeyError(belief_id)
        for index, record in enumerate(versions[:-1]):
            record["status"] = "superseded"
            record["superseded_at"] = versions[index + 1]["recorded_at"]
        return [_public(record) for record in versions]

    def why(self, belief_id: str) -> dict:
        history = self.history(belief_id)
        current = history[-1]
        return {
            "id": belief_id,
            "version_id": current["version_id"],
            "claim": current["claim"],
            "evidence": current["evidence"],
            "links": current["links"],
            "assumptions": current["assumptions"],
        }

    def changed(self, since: str | datetime, until: str | datetime | None = None) -> list[dict]:
        start = _time(since)
        end = _time(until) if until is not None else datetime.max.replace(tzinfo=UTC)
        if start > end:
            raise BeliefValidationError("since must not be later than until")
        return [
            _public(event["belief"])
            for event in self._records()
            if start < _time(event["recorded_at"]) <= end
        ]

    def review(self, kind: str = "queue", *, at: str | datetime | None = None) -> list[dict]:
        """Read the queue without returning diagnostic flags or revising a rank."""
        if kind not in {"queue", "conflicts", "stale"}:
            raise BeliefValidationError("review kind must be queue, conflicts, or stale")
        now = _time(at or self._clock())
        records = self._fold(self._records()).values()
        result = []
        for record in records:
            if record["status"] not in _OPEN_STATUSES:
                continue
            stale = (
                record.get("retention_until") is not None
                and _time(record["retention_until"]) <= now
            ) or now >= _time(record["last_support_at"] or record["recorded_at"]) + timedelta(
                days=_HORIZONS[record["change_class"]]
            )
            if (
                (kind == "queue" and (record["status"] in {"review", "conditional"} or stale))
                or (kind == "conflicts" and "code_detected_conflict" in record["flags"])
                or (kind == "stale" and stale)
            ):
                result.append(_public(record))
        return sorted(
            result,
            key=lambda item: (
                (item.get("triple") or {}).get("subject", ""),
                item["recorded_at"],
                item["id"],
            ),
        )

    def verify(self) -> dict:
        try:
            events = self._records()
        except BeliefIntegrityError as error:
            return {"valid": False, "error": str(error)}
        return {
            "valid": True,
            "count": len(events),
            "head": events[-1]["hash"] if events else _GENESIS,
        }

    def render(self, *, records: list[dict] | None = None) -> dict[str, str]:
        """Regenerate agent-readable views; private diagnostics are never copied."""
        events = self._records()
        records = (
            [_public(record) for record in self._fold(events).values()]
            if records is None
            else copy.deepcopy(records)
        )
        ordered = sorted(
            records,
            key=lambda item: (
                (item.get("triple") or {}).get("subject", ""),
                item["recorded_at"],
                item["id"],
            ),
        )
        ledger = "".join(_canonical(_public(event["belief"])) + "\n" for event in events)
        sheet = (
            "# Beliefs\n\n"
            + "\n\n".join(
                f"## {record['id']}\n\n{record['claim']}\n\n"
                f"Kind: {record['kind']}; rank: {record['rank']}; status: {record['status']}.\n"
                f"Valid from: {record['valid_from']}; recorded at: {record['recorded_at']}.\n"
                + (f"Valid to: {record['valid_to']}.\n" if record.get("valid_to") else "")
                + "Evidence: "
                + ", ".join(reference["ref"] for reference in record["evidence"])
                for record in ordered
            )
            + "\n"
        )
        core_records = [
            record
            for record in ordered
            if (
                record["status"] in _OPEN_STATUSES
                and (
                    (record["kind"] in {"rule", "self"} and record["rank"] >= 3)
                    or record["status"] in {"review", "conditional"}
                    or bool(record.get("kernel", {}).get("support_lost"))
                    or bool(record.get("kernel", {}).get("classifications"))
                )
            )
        ]
        core = "# Core beliefs\n\n"
        for record in core_records:
            line = f"- [{record['id']}] {record['claim']} (rank {record['rank']}; {record['status']})\n"
            if len((core + line).encode("utf-8")) > 2000:
                break
            core += line
        self.views_dir.mkdir(parents=True, exist_ok=True)
        views = {"ledger.jsonl": ledger, "sheet.md": sheet, "core.md": core}
        for name, content in views.items():
            descriptor, temporary = tempfile.mkstemp(dir=self.views_dir, prefix=".render-")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o444)
                os.replace(temporary, self.views_dir / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {name: str(self.views_dir / name) for name in views}
