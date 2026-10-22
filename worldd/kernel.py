"""Persistent belief boundary, mechanical late-evidence kernel and pattern bundles."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from .beliefs import (
    BeliefIntegrityError,
    BeliefStore,
    BeliefValidationError,
    _canonical,
    _hash,
    _public,
    _stamp,
    _time,
)
from .util import blocked, identifier, now

SCHEMA_ID = "belief@1"
VALIDATOR_ID = "structural-private-flags@1"
KERNEL_ID = "mechanical-depth3-n50-assumptions4-open64@1"
SLOT_SUGGESTER_ID = "copula-slots@1"
_FINAL_RUN_STATES = {"sealed", "killed", "ended"}
_APPROVED = {"approved", "deposited", "complete", "held", True}
_DENIED = {"rejected", "expired", "cancelled", "ignored", "missed", False}


class ExtensionDB:
    """Small WAL database and hash journal shared by belief-related services.

    Background outcomes have their own journal, so they never append behind an
    episode's seal. The original record receives a reference while it is open.
    """

    def __init__(self, store):
        self.store = store
        self.path = Path(store.data_dir) / "belief-services.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS belief_observations (
                run_id TEXT NOT NULL, observation_ref TEXT NOT NULL,
                observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id, observation_ref)
            );
            CREATE TABLE IF NOT EXISTS belief_imports (
                run_id TEXT NOT NULL, ref TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id,ref)
            );
            CREATE TABLE IF NOT EXISTS belief_journal (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, hash TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(run_id,seq)
            );
            CREATE TABLE IF NOT EXISTS belief_skills (
                run_id TEXT NOT NULL, name TEXT NOT NULL, version_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(run_id,version_id)
            );
            CREATE TABLE IF NOT EXISTS belief_notes (
                run_id TEXT NOT NULL, ref TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id,ref)
            );
            """
        )
        self.path.chmod(0o600)

    def audit(self, run_id: str, kind: str, **fields) -> dict:
        self.store.get_run(run_id)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                head = self.db.execute(
                    "SELECT seq,hash FROM belief_journal WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                event = {
                    "seq": head["seq"] + 1 if head else 1,
                    "run_id": run_id,
                    "kind": kind,
                    "recorded_at": now(),
                    "event_id": identifier("ke"),
                    "prev_hash": head["hash"] if head else "sha256:" + "0" * 64,
                    **fields,
                }
                event["hash"] = _hash(event)
                self.db.execute(
                    "INSERT INTO belief_journal VALUES(?,?,?,?)",
                    (run_id, event["seq"], event["hash"], _canonical(event)),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        if self.store.get_run(run_id)["state"] not in _FINAL_RUN_STATES:
            self.store.append_record(
                run_id,
                kind,
                extension_event_id=event["event_id"],
                extension_hash=event["hash"],
                **fields,
            )
        return event

    def journal(self, run_id: str) -> list[dict]:
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload FROM belief_journal WHERE run_id=? ORDER BY seq",
                    (run_id,),
                )
            ]

    def verify(self, run_id: str) -> dict:
        previous = "sha256:" + "0" * 64
        events = self.journal(run_id)
        for sequence, event in enumerate(events, 1):
            supplied_hash = event["hash"]
            content = {key: value for key, value in event.items() if key != "hash"}
            if (
                event["seq"] != sequence
                or event["prev_hash"] != previous
                or _hash(content) != supplied_hash
            ):
                return {"valid": False, "error": f"broken kernel journal at {sequence}"}
            previous = supplied_hash
        return {"valid": True, "count": len(events), "head": previous}

    def close(self):
        self.db.close()


class BeliefService:
    """The free belief boundary plus a deterministic, non-epistemic kernel.

    ``observe`` is a trusted world-side API. Tool input cannot supply its world
    timestamps or mark arbitrary strings as resolved evidence.
    """

    def __init__(self, world, *, clock=None, trusted_bundle_keys: dict[str, bytes] | None = None):
        self.world = world
        self.store = world.store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.database = ExtensionDB(self.store)
        self._stores: dict[str, BeliefStore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._trusted_bundle_keys = trusted_bundle_keys or {}
        key_dir = Path(self.store.data_dir) / "keys"
        key_dir.mkdir(mode=0o700, exist_ok=True)
        key_path = key_dir / "belief-bundles.key"
        try:
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if key_path.is_symlink():
                raise BeliefValidationError("bundle signing key must not be a symlink")
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(os.urandom(32))
                output.flush()
                os.fsync(output.fileno())
        self._bundle_key = key_path.read_bytes()
        self._key_id = "sha256:" + hashlib.sha256(self._bundle_key).hexdigest()

    def _config(self, run_id):
        return self.store.get_run(run_id)["config"]

    def suggest_slots(self, run_id: str, claim: str) -> dict:
        """Propose slots from one conservative surface grammar; never accept or judge them."""
        policy = self._config(run_id).get("slot_suggester_policy_id", SLOT_SUGGESTER_ID)
        if policy not in {SLOT_SUGGESTER_ID, "none"}:
            raise BeliefValidationError("unknown slot suggester policy")
        result = {
            "policy_id": policy,
            "status": "unavailable",
            "suggested_slots": None,
            "auto_accepted": False,
            "grammar_only": True,
        }
        if policy == "none":
            return {**result, "status": "disabled"}
        if not isinstance(claim, str) or not 1 <= len(claim) <= 300:
            return result
        surface = claim.strip().removesuffix(".").strip()
        if re.search(r"[\n\r;:?!\"'“”‘’]", surface) or re.search(
            r"\b(?:and|or|if|when|unless|because|although|while|that|which)\b",
            surface,
            re.IGNORECASE,
        ):
            return result
        if re.search(r"(?<!\d)\.|\.(?!\d)", surface):
            return result
        copulas = list(re.finditer(r"\b(?:is|are|was|were|equals)\b", surface, re.IGNORECASE))
        if len(copulas) + surface.count("=") != 1:
            return result
        if copulas:
            match = re.fullmatch(r"(.+?)\s+(is|are|was|were|equals)\s+(.+)", surface, re.IGNORECASE)
            if not match:
                return result
            subject, predicate, object_ = match.groups()
            predicate = predicate.lower()
        else:
            match = re.fullmatch(r"([^=<>!]+?)\s*=\s*([^=<>!]+)", surface)
            if not match:
                return result
            subject, object_ = match.groups()
            predicate = "="
        subject, object_ = subject.strip(), object_.strip()
        if (
            not subject
            or not object_
            or len(subject.split()) > 12
            or len(object_.split()) > 24
            or subject.casefold()
            in {"it", "this", "these", "those", "he", "she", "they", "there", "here"}
        ):
            return result
        return {
            **result,
            "status": "proposed",
            "suggested_slots": {"subject": subject, "predicate": predicate, "object": object_},
        }

    @staticmethod
    def _note_ref(ref):
        if not isinstance(ref, str):
            return None
        match = re.fullmatch(r"(?:note:)?sha256:([0-9a-f]{64})", ref)
        return "note:sha256:" + match[1] if match else None

    def _note_location(self, run_id, ref):
        return self.store.run_dir(run_id) / "beliefs" / "notes" / ref.rsplit(":", 1)[1]

    def _verified_note(self, receipt):
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        base = self._note_location(receipt["run_id"], receipt["ref"])
        body, metadata = base.with_suffix(".bin"), base.with_suffix(".json")
        if (
            receipt.get("ref") != self._note_ref(receipt.get("hash", ""))
            or receipt.get("evidence_origin") != "workspace_note"
            or receipt.get("class") != "self_inference"
            or _hash(unsigned) != receipt.get("receipt_hash")
            or body.is_symlink()
            or metadata.is_symlink()
            or not body.is_file()
            or not metadata.is_file()
            or metadata.read_text() != _canonical(receipt)
        ):
            raise BeliefIntegrityError("workspace note receipt integrity failed")
        hasher, length = hashlib.sha256(), 0
        with body.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
                length += len(chunk)
        if "sha256:" + hasher.hexdigest() != receipt["hash"] or length != receipt["bytes"]:
            raise BeliefIntegrityError("workspace note snapshot byte integrity failed")
        return copy.deepcopy(receipt)

    def get_note(self, run_id: str, ref: str) -> dict:
        """Read an immutable receipt in the current run, series or life, without parsing bytes."""
        normalized = self._note_ref(ref)
        if normalized is None:
            raise KeyError(ref)
        config = self._config(run_id)
        scope = config.get("series_id") or config.get("life_id")
        candidates = sorted(self.store.list_runs(), key=lambda run: run["run_id"] != run_id)
        for candidate in candidates:
            if candidate["run_id"] != run_id and (
                not scope
                or (candidate["config"].get("series_id") or candidate["config"].get("life_id"))
                != scope
            ):
                continue
            with self.database.lock:
                row = self.database.db.execute(
                    "SELECT payload FROM belief_notes WHERE run_id=? AND ref=?",
                    (candidate["run_id"], normalized),
                ).fetchone()
            if row:
                receipt = self._verified_note(json.loads(row[0]))
                return {**receipt, "series_id": scope}
        raise KeyError(ref)

    def note_bytes(self, run_id: str, ref: str) -> bytes:
        """World-only access to the exact captured version, never the edited workspace file."""
        receipt = self.get_note(run_id, ref)
        body = self._note_location(receipt["run_id"], receipt["ref"]).with_suffix(".bin")
        content = body.read_bytes()
        if "sha256:" + hashlib.sha256(content).hexdigest() != receipt["hash"]:
            raise BeliefIntegrityError("workspace note snapshot changed during read")
        return content

    def _capture_note(self, run_id, path, expected_hash=None):
        root = self.store.run_dir(run_id) / "work"
        supplied = Path(path)
        if supplied.is_absolute():
            try:
                relative = supplied.relative_to("/work")
            except ValueError:
                try:
                    relative = supplied.relative_to(root)
                except ValueError:
                    raise BeliefValidationError("workspace notes must be under /work") from None
        else:
            relative = supplied
        if not relative.parts or any(part in {"..", ""} for part in relative.parts):
            raise BeliefValidationError("workspace note path must identify a file under /work")
        private = self.store.run_dir(run_id) / "beliefs" / "notes"
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        if private.is_symlink():
            raise BeliefIntegrityError("private note directory must not be a symlink")
        temporary_fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=private)
        descriptor = None
        directory = None
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in relative.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise BeliefValidationError("workspace notes must be regular files without links")
            hasher, size = hashlib.sha256(), 0
            with os.fdopen(temporary_fd, "wb") as target:
                temporary_fd = None
                while chunk := os.read(descriptor, 1024 * 1024):
                    target.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or size != after.st_size:
                raise BeliefValidationError("workspace note changed while being captured")
            content_hash = "sha256:" + hasher.hexdigest()
            if expected_hash and self._note_ref(expected_hash) != self._note_ref(content_hash):
                return None
            normalized = self._note_ref(content_hash)
            with self.database.lock:
                existing = self.database.db.execute(
                    "SELECT payload FROM belief_notes WHERE run_id=? AND ref=?",
                    (run_id, normalized),
                ).fetchone()
                if existing:
                    return self._verified_note(json.loads(existing[0]))
                base = self._note_location(run_id, normalized)
                body, metadata = base.with_suffix(".bin"), base.with_suffix(".json")
                try:
                    os.link(temporary, body)
                except FileExistsError:
                    if (
                        body.is_symlink()
                        or "sha256:" + hashlib.sha256(body.read_bytes()).hexdigest() != content_hash
                    ):
                        raise BeliefIntegrityError(
                            "existing workspace note snapshot is corrupt"
                        ) from None
                body.chmod(0o400)
                receipt = {
                    "ref": normalized,
                    "hash": content_hash,
                    "bytes": size,
                    "run_id": run_id,
                    "recorded_at": _stamp(self.clock()),
                    "evidence_origin": "workspace_note",
                    "class": "self_inference",
                    "workspace_path": relative.as_posix(),
                    "content_parsed": False,
                }
                receipt["receipt_hash"] = _hash(receipt)
                if metadata.exists():
                    receipt = json.loads(metadata.read_text())
                    self._verified_note(receipt)
                else:
                    with metadata.open("x") as output:
                        output.write(_canonical(receipt))
                        output.flush()
                        os.fsync(output.fileno())
                    metadata.chmod(0o400)
                private_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(private_fd)
                finally:
                    os.close(private_fd)
                self.database.audit(run_id, "workspace_note_registered", receipt=receipt)
                self.database.db.execute(
                    "INSERT INTO belief_notes VALUES(?,?,?)",
                    (run_id, normalized, _canonical(receipt)),
                )
                return copy.deepcopy(receipt)
        except OSError as error:
            raise BeliefValidationError(
                "workspace note must be an accessible regular file without symlinks"
            ) from error
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)
            Path(temporary).unlink(missing_ok=True)

    def register_note(self, run_id: str, path: str, *, expected_hash=None) -> dict:
        """Snapshot regular workspace bytes, without interpreting or extracting any content."""
        if self.store.get_run(run_id)["state"] != "running":
            raise BeliefValidationError(
                "workspace notes can be registered only in a running session"
            )
        if not isinstance(path, str) or not path or "\x00" in path:
            raise BeliefValidationError("workspace note path must be nonempty text")
        if expected_hash is not None and (
            not isinstance(expected_hash, str) or self._note_ref(expected_hash) is None
        ):
            raise BeliefValidationError("expected note hash must be a SHA-256 reference")
        receipt = self._capture_note(run_id, path, expected_hash)
        if receipt is None:
            raise BeliefValidationError("workspace note bytes do not match the expected hash")
        return receipt

    def _auto_note(self, run_id, ref):
        if self._note_ref(ref) is None or self.store.get_run(run_id)["state"] != "running":
            return None
        root = self.store.run_dir(run_id) / "work"
        if not root.is_dir() or root.is_symlink():
            return None
        for directory, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories if not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                try:
                    receipt = self._capture_note(run_id, path.relative_to(root), expected_hash=ref)
                except BeliefValidationError:
                    continue
                if receipt:
                    return {
                        **receipt,
                        "series_id": self._config(run_id).get("series_id")
                        or self._config(run_id).get("life_id"),
                    }
        return None

    def _resolve_evidence(self, run_id: str, ref: str) -> dict | None:
        config = self._config(run_id)
        series = config.get("series_id") or config.get("life_id")
        life_id = config.get("life_id")
        if self._note_ref(ref):
            try:
                return self.get_note(run_id, ref)
            except KeyError:
                pass
        asks = getattr(self.world, "services", {}).get("asks")
        if asks is not None and ref.startswith("t_"):
            try:
                ticket = asks.get(ref)
                ticket_source = self.store.get_run(ticket["run_id"])
                if ticket.get("run_id") == run_id or (
                    life_id and ticket_source["config"].get("life_id") == life_id
                ):
                    return {**ticket, "recorded_at": ticket["opened_at"], "series_id": series}
            except KeyError:
                pass
        for candidate in self.store.list_runs():
            if candidate["run_id"] != run_id and (
                not series
                or (candidate["config"].get("series_id") or candidate["config"].get("life_id"))
                != series
            ):
                continue
            for record in reversed(self.store.records(candidate["run_id"])):
                identities = [
                    record.get(key)
                    for key in (
                        "action_id",
                        "outcome_id",
                        "event_id",
                        "ticket_id",
                        "extension_event_id",
                    )
                ]
                nested = record.get("event", {})
                if isinstance(nested, dict):
                    identities.extend(nested.get(key) for key in ("event_id", "ticket_id"))
                if ref in identities:
                    return {
                        **record,
                        "recorded_at": record["ts"],
                        "series_id": candidate["config"].get("series_id")
                        or candidate["config"].get("life_id"),
                    }
        with self.database.lock:
            normalized = self._note_ref(ref)
            row = self.database.db.execute(
                "SELECT payload FROM belief_imports WHERE run_id=? AND ref IN (?,?,?)",
                (
                    run_id,
                    ref,
                    normalized or ref,
                    normalized.removeprefix("note:") if normalized else ref,
                ),
            ).fetchone()
        if row:
            return json.loads(row[0])
        for event in self.database.journal(run_id):
            if ref in {event.get("event_id"), event.get("observation_ref")}:
                return {**event, "series_id": series}
        return self._auto_note(run_id, ref)

    def beliefs(self, run_id: str) -> BeliefStore:
        if run_id not in self._stores:
            config = self._config(run_id)
            self._stores[run_id] = BeliefStore(
                self.store.data_dir,
                run_id,
                lambda ref: self._resolve_evidence(run_id, ref),
                series_id=config.get("series_id") or config.get("life_id"),
                clock=self.clock,
                enable_flags=config.get("validator_policy_id")
                not in {"structural-no-flags@1", "no_flags"},
                require_fact_triples=config.get("belief_schema_id") != "belief-prose-no-slots@1",
            )
        return self._stores[run_id]

    def _observations(self, run_id, as_of=None):
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT payload FROM belief_observations WHERE run_id=? ORDER BY observed_at,observation_ref",
                (run_id,),
            ).fetchall()
        return [
            json.loads(row[0])
            for row in rows
            if as_of is None or _time(json.loads(row[0])["recorded_at"]) <= _time(as_of)
        ]

    @staticmethod
    def _attachments(state: dict, observation: dict) -> list[str]:
        targets = []
        routes = {observation.get(key) for key in ("action_id", "about_action", "ticket_id")}
        routes.discard(None)
        explicit = observation.get("belief_id")
        triple = observation.get("triple")
        instant = _time(observation["observed_at"])
        for belief_id, record in state.items():
            if record["status"] in {"retracted", "superseded"}:
                continue
            evidence_routes = {item["ref"] for item in record["evidence"]} | set(
                record["assumptions"]
            )
            old_triple = record.get("triple")
            structured = (
                isinstance(triple, dict)
                and old_triple
                and triple.get("subject") == old_triple["subject"]
                and triple.get("predicate") == old_triple["predicate"]
                and _time(record["valid_from"]) <= instant
                and (not record.get("valid_to") or instant < _time(record["valid_to"]))
            )
            if explicit == belief_id or routes & evidence_routes or structured:
                targets.append(belief_id)
        return sorted(targets)

    def fold(self, run_id: str, as_of=None, *, valid_at=None, include_flags=False) -> list[dict]:
        state = {record["id"]: record for record in self.beliefs(run_id).fold(as_of)}
        for record in state.values():
            record["kernel"] = record.pop(
                "imported_kernel",
                {
                    "evidence": [],
                    "support_lost": [],
                    "assumption_outcomes": {},
                    "classifications": [],
                    "flags": [],
                },
            )
            record["kernel"].setdefault("flags", [])
        for observation in self._observations(run_id, as_of):
            attached = self._attachments(state, observation)[:50]
            affected = set(attached)
            contradictions = []
            for belief_id in attached:
                record = state[belief_id]
                kernel = record["kernel"]
                ref = observation["observation_ref"]
                ticket = observation.get("ticket_id")
                if ticket in record["assumptions"] and "outcome" in observation:
                    outcome = observation["outcome"]
                    kernel["assumption_outcomes"][ticket] = outcome
                    outcomes = kernel["assumption_outcomes"]
                    if any(outcomes.get(item) in _DENIED for item in record["assumptions"]):
                        record["status"] = "expired"
                    elif all(outcomes.get(item) in _APPROVED for item in record["assumptions"]):
                        record["status"] = "proposed" if record["author"] == "reflector" else "held"
                triple = observation.get("triple")
                old_triple = record.get("triple")
                contradicted = observation.get("supports") is False or bool(
                    triple
                    and old_triple
                    and triple.get("subject") == old_triple["subject"]
                    and triple.get("predicate") == old_triple["predicate"]
                    and triple.get("object") != old_triple["object"]
                )
                semantic_direction = (
                    not contradicted
                    if (
                        "supports" in observation
                        or (
                            triple
                            and old_triple
                            and triple.get("subject") == old_triple["subject"]
                            and triple.get("predicate") == old_triple["predicate"]
                        )
                    )
                    else None
                )
                kernel["evidence"].append(
                    {
                        "ref": ref,
                        "class": observation.get("class", "observed"),
                        "observed_at": observation["observed_at"],
                        "supports": semantic_direction,
                    }
                )
                if semantic_direction is True:
                    record["n_support"] += 1
                    record["last_support_at"] = max(
                        record.get("last_support_at") or observation["observed_at"],
                        observation["observed_at"],
                    )
                if contradicted:
                    record["n_contra"] += 1
                    classification = (
                        "revision"
                        if record["change_class"] in {"static", "false_premise"}
                        else "update"
                    )
                    kernel["classifications"].append(
                        {
                            "observation_ref": ref,
                            "kind": classification,
                            "observed_at": observation["observed_at"],
                        }
                    )
                    kernel["flags"].append("contradicting_evidence")
                    if classification == "revision":
                        kernel["flags"].append("source_support_lost")
                        kernel.setdefault("source_feedback", []).extend(
                            {
                                "source_ref": reference["ref"],
                                "direction": "lower",
                                "observation_ref": ref,
                            }
                            for reference in record["evidence"]
                        )
                    elif _time(record["valid_from"]) < _time(observation["observed_at"]):
                        record["valid_to"] = min(
                            record.get("valid_to") or observation["observed_at"],
                            observation["observed_at"],
                        )
                    contradictions.append(belief_id)
            queue = deque((belief_id, 0) for belief_id in contradictions)
            visited = set(contradictions)
            while queue:
                source, depth = queue.popleft()
                if depth >= 3:
                    continue
                children = sorted(
                    belief_id
                    for belief_id, record in state.items()
                    if record["status"] not in {"retracted", "superseded"}
                    and {"to": source, "relation": "derived_from"} in record["links"]
                )
                for child in children:
                    if child in visited or (child not in affected and len(affected) >= 50):
                        continue
                    visited.add(child)
                    affected.add(child)
                    state[child]["kernel"]["support_lost"].append(
                        {
                            "observation_ref": observation["observation_ref"],
                            "from": source,
                            "hop": depth + 1,
                        }
                    )
                    state[child]["kernel"]["flags"].append("support_lost")
                    queue.append((child, depth + 1))
        result = []
        instant = _time(valid_at) if valid_at is not None else None
        for belief_id in sorted(state):
            record = state[belief_id]
            if instant is not None and not (
                _time(record["valid_from"]) <= instant
                and (not record.get("valid_to") or instant < _time(record["valid_to"]))
            ):
                continue
            if not include_flags:
                record["kernel"].pop("flags", None)
            elif not self.beliefs(run_id).enable_flags:
                record["kernel"]["flags"] = []
            result.append(record)
        return result

    async def observe(self, run_id: str, observation: dict) -> dict:
        """Attach a trusted world observation, idempotently; never accept tool input here."""
        self.store.get_run(run_id)
        payload = copy.deepcopy(observation)
        recorded_at = _stamp(self.clock())
        payload["recorded_at"] = recorded_at
        payload["observed_at"] = _stamp(payload.get("observed_at", recorded_at))
        if _time(payload["observed_at"]) > _time(recorded_at):
            raise BeliefValidationError("world observation cannot postdate recording")
        payload.setdefault("observation_ref", identifier("ko"))
        if not isinstance(payload["observation_ref"], str):
            raise BeliefValidationError("observation_ref must be text")
        allowed = {
            "observed",
            "human",
            "counterparty",
            "fetched_content",
            "vault",
            "self_inference",
        }
        if payload.get("class", "observed") not in allowed:
            raise BeliefValidationError("invalid evidence class")
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            with self.database.lock:
                prior = self.database.db.execute(
                    "SELECT payload FROM belief_observations WHERE run_id=? AND observation_ref=?",
                    (run_id, payload["observation_ref"]),
                ).fetchone()
                if prior:
                    previous = json.loads(prior[0])
                    content = {key: value for key, value in payload.items() if key != "recorded_at"}
                    old = {key: value for key, value in previous.items() if key != "recorded_at"}
                    if content != old:
                        raise BeliefValidationError("observation ids are immutable")
                    return {
                        "kind": "silence",
                        "observation_ref": payload["observation_ref"],
                        "duplicate": True,
                    }
                self.database.db.execute(
                    "INSERT INTO belief_observations VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        payload["observation_ref"],
                        payload["observed_at"],
                        recorded_at,
                        _canonical(payload),
                    ),
                )
            event = self.database.audit(
                run_id,
                "belief_observation",
                observation=payload,
                observation_ref=payload["observation_ref"],
            )
            projection = self.fold(run_id, include_flags=True)
            affected = [
                record
                for record in projection
                if any(
                    item["ref"] == payload["observation_ref"]
                    for item in record["kernel"]["evidence"]
                )
                or any(
                    item["observation_ref"] == payload["observation_ref"]
                    for item in record["kernel"]["support_lost"]
                )
            ]
            self.render(run_id)
            policy = self._config(run_id).get("reflection_policy", "none")
            if affected and policy == "self":
                self.world.push(
                    run_id,
                    {
                        "type": "belief_review",
                        "observation_ref": payload["observation_ref"],
                        "belief_ids": [record["id"] for record in affected],
                    },
                )
            return {
                "kind": "silence",
                "observation_ref": payload["observation_ref"],
                "affected": len(affected),
                "journal_ref": event["event_id"],
            }

    async def observe_event(self, run_id: str, event: dict) -> dict:
        """Normalize a world outcome without interpreting prose as a proposition."""
        config = self._config(run_id)
        if "belief" not in config.get("packs", []) and run_id not in self._stores:
            return {"kind": "silence", "reason": "belief_pack_disabled"}
        observation = copy.deepcopy(event)
        if observation.get("type") == "belief_review" and not observation.get("source_run_id"):
            with self.database.lock:
                existing = self.database.db.execute(
                    "SELECT 1 FROM belief_observations WHERE run_id=? AND observation_ref=?",
                    (run_id, observation.get("observation_ref")),
                ).fetchone()
            if existing:
                return {"kind": "silence", "reason": "observation_already_attached"}
        if observation.get("type") == "belief_review" and observation.get("source_run_id"):
            with self.database.lock:
                original = self.database.db.execute(
                    "SELECT payload FROM belief_observations WHERE run_id=? AND observation_ref=?",
                    (observation["source_run_id"], observation.get("observation_ref")),
                ).fetchone()
            if original:
                observation = json.loads(original[0])
        expectation = observation.get("expectation")
        if observation.get("type") == "expectation_resolved" and isinstance(expectation, dict):
            observation.setdefault("about_action", expectation.get("about_action"))
            if expectation.get("outcome") in {"held", "missed"}:
                observation["supports"] = expectation["outcome"] == "held"
        if observation.get("type") == "ask_resolved" and "outcome" not in observation:
            observation["outcome"] = observation.get("state")
        return await self.observe(run_id, observation)

    def _check_assumptions(self, run_id, payload):
        for ticket_id in payload.get("assumptions", []):
            services = getattr(self.world, "services", {})
            asks = services.get("asks") or services.get("ask")
            ticket = None
            if asks is not None:
                getter = getattr(asks, "get", None) or getattr(asks, "get_ticket", None)
                if getter:
                    try:
                        ticket = getter(ticket_id)
                    except (KeyError, TypeError):
                        try:
                            ticket = getter(run_id, ticket_id)
                        except (KeyError, TypeError):
                            pass
            if not ticket:
                ticket = self._resolve_evidence(run_id, ticket_id)
            ticket_run = ticket.get("run_id", run_id) if ticket else None
            same_life = bool(
                ticket
                and self._config(run_id).get("life_id")
                and self.store.get_run(ticket_run)["config"].get("life_id")
                == self._config(run_id)["life_id"]
            )
            if not ticket or (ticket_run != run_id and not same_life):
                raise BeliefValidationError("assumption must refer to a ticket in this run")
            if ticket.get("state", "open") not in {"open", "accepted"}:
                raise BeliefValidationError("assumption must refer to an open ticket")

    async def handle(
        self, run_id: str, action_id: str, tool: str, params: dict, *, author="agent"
    ) -> dict:
        beliefs = self.beliefs(run_id)
        try:
            if tool in {"believe", "revise"}:
                payload = (
                    params.get("record", params) if tool == "believe" else params.get("delta", {})
                )
                payload = copy.deepcopy(payload)
                if payload.get("author", author) != author:
                    raise BeliefValidationError(
                        "belief authorship is assigned by the caller's lane"
                    )
                if author == "reflector" and tool != "believe":
                    raise BeliefValidationError("reflector may propose beliefs only")
                if tool == "believe":
                    payload["author"] = author
                self._check_assumptions(run_id, payload)
                # Stamp a newly cited note before the semantic transaction takes its time.
                references = payload.get("evidence", [])
                if isinstance(references, list):
                    for reference in references:
                        if isinstance(reference, dict) and self._note_ref(reference.get("ref")):
                            self._resolve_evidence(run_id, reference["ref"])
            if tool == "believe":
                result = beliefs.believe(payload)
            elif tool == "revise":
                prior_classifications = self.query(run_id, query="why", id=params["id"])["kernel"][
                    "classifications"
                ]
                old = next(
                    (record for record in beliefs.fold() if record["id"] == params["id"]), None
                )
                if old is None:
                    raise KeyError(params["id"])
                if old["author"] == "reflector" and author == "agent":
                    if payload == {"status": "held"}:
                        result = beliefs.reconfirm(params["id"])
                    else:
                        result = beliefs.revise(params["id"], {**payload, "author": "agent"})
                else:
                    result = beliefs.revise(params["id"], payload)
            elif tool == "retract":
                result = beliefs.retract(params["id"])
            elif tool == "link":
                result = beliefs.link(
                    params.get("from", params.get("from_id")),
                    params.get("to", params.get("to_id")),
                    params["relation"],
                )
            elif tool == "beliefs":
                return {"kind": "result", "data": self.query(run_id, **params)}
            else:
                return blocked("policy_deny", "Unknown belief tool", "belief_tool")
            self.database.audit(
                run_id,
                "belief_" + result["op"],
                belief_id=result["id"],
                version_id=result["version_id"],
                action_id=action_id,
            )
            if tool == "revise" and isinstance(payload.get("revision"), dict):
                classification = prior_classifications
                if classification:
                    expected = classification[-1]["kind"]
                    override = payload["revision"]["kind"]
                    self.database.audit(
                        run_id,
                        "belief_revision_override",
                        belief_id=result["id"],
                        kernel_kind=expected,
                        agent_kind=override,
                        disagreement=expected != override,
                        observation_ref=classification[-1]["observation_ref"],
                    )
            self.render(run_id)
            return {"kind": "result", "data": result}
        except (
            BeliefValidationError,
            BeliefIntegrityError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            extras = {}
            if (
                tool == "believe"
                and str(error) == "facts require a subject, predicate, object triple"
            ):
                candidate = self.suggest_slots(run_id, payload.get("claim"))
                if candidate["status"] == "proposed":
                    extras = {
                        "suggested_slots": candidate["suggested_slots"],
                        "slot_suggester_policy_id": candidate["policy_id"],
                        "suggestion_accepted": False,
                    }
            return blocked("policy_deny", str(error), "belief_structural_validation", **extras)

    def query(self, run_id, query="all", **params):
        beliefs = self.beliefs(run_id)
        if isinstance(query, dict):
            return self.query(run_id, **query)
        if query == "suggest_slots":
            return self.suggest_slots(run_id, params.get("claim"))
        if query == "all":
            return self.fold(run_id, params.get("as_of"), valid_at=params.get("valid_at"))
        if query == "about":
            entity = params["entity"]
            return [
                record
                for record in self.fold(
                    run_id, params.get("as_of"), valid_at=params.get("valid_at")
                )
                if record["status"] != "retracted"
                and (
                    entity.casefold() in record["claim"].casefold()
                    or entity in (record.get("triple") or {}).values()
                )
            ]
        if query == "why":
            record = next(
                (record for record in self.fold(run_id) if record["id"] == params["id"]), None
            )
            if record is None:
                raise KeyError(params["id"])
            return record
        if query == "history":
            return beliefs.history(params["id"])
        if query == "changed":
            return beliefs.changed(params["since"], params.get("until"))
        if query == "review":
            kind = params.get("kind", "queue")
            existing = {record["id"]: record for record in beliefs.review(kind)}
            for record in self.fold(run_id, include_flags=True):
                if record["kernel"]["flags"] and kind in {"queue", "conflicts"}:
                    record["kernel"].pop("flags", None)
                    existing[record["id"]] = record
            return sorted(
                existing.values(),
                key=lambda record: (
                    (record.get("triple") or {}).get("subject", ""),
                    record["recorded_at"],
                    record["id"],
                ),
            )
        raise BeliefValidationError("unknown belief query")

    def render(self, run_id):
        beliefs = self.beliefs(run_id)
        paths = beliefs.render(records=self.fold(run_id))
        # Current mechanical projections are separate from the append-only ledger.
        from tempfile import NamedTemporaryFile

        target = beliefs.views_dir / "state.json"
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=beliefs.views_dir, delete=False
        ) as output:
            output.write(_canonical(self.fold(run_id)))
            temporary = Path(output.name)
        temporary.chmod(0o444)
        os.replace(temporary, target)
        return {**paths, "state.json": str(target)}

    def initialize_source_trust(self, run_id: str) -> dict:
        """Install the registered, revisable source-trust prior for a fresh store."""
        beliefs = self.beliefs(run_id)
        if beliefs.fold():
            raise BeliefValidationError("source-trust initialization requires fresh beliefs")
        condition = self.database.audit(
            run_id, "belief_initial_condition", condition="source_trust@1"
        )
        rule = beliefs.believe(
            {
                "kind": "rule",
                "claim": "Initially prefer observed evidence to human reports, human reports to counterparty reports, and counterparty reports to self-inference; this rule is revisable.",
                "rank": 3,
                "evidence": [{"ref": condition["event_id"], "class": "observed"}],
            }
        )
        self.database.audit(
            run_id,
            "belief_prior_installed",
            belief_id=rule["id"],
            condition="source_trust@1",
            initial_condition=True,
        )
        self.render(run_id)
        return rule

    def promote_skill(
        self, run_id: str, name: str, source: str, tests: str, test_evidence_ref: str
    ) -> dict:
        """Promote code only against a world-owned passing test result for its exact hashes.

        The harness/sandbox runner records ``skill_test`` with ``passed``,
        ``tests_executed``, ``source_hash``, ``tests_hash`` and an ``event_id``.
        Merely claiming that a test passed in a tool argument cannot promote code.
        """
        import ast

        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", name):
            raise BeliefValidationError("invalid skill name")
        if (
            not isinstance(source, str)
            or not isinstance(tests, str)
            or len((source + tests).encode()) > 500000
        ):
            raise BeliefValidationError("skill code and tests must be bounded text")
        try:
            ast.parse(source)
            ast.parse(tests)
        except SyntaxError as error:
            raise BeliefValidationError(
                "skill source or tests have invalid Python syntax"
            ) from error
        digest_source = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        digest_tests = "sha256:" + hashlib.sha256(tests.encode()).hexdigest()
        result = self._resolve_evidence(run_id, test_evidence_ref)
        if (
            not result
            or result.get("kind") != "skill_test"
            or result.get("passed") is not True
            or not isinstance(result.get("tests_executed"), int)
            or result["tests_executed"] < 1
            or result.get("source_hash") != digest_source
            or result.get("tests_hash") != digest_tests
        ):
            raise BeliefValidationError(
                "skill lacks a passing world test for the exact source and test hashes"
            )
        skill = {
            "name": name,
            "version_id": identifier("skill"),
            "source": source,
            "tests": tests,
            "source_hash": digest_source,
            "tests_hash": digest_tests,
            "test_evidence_ref": test_evidence_ref,
            "tests_executed": result["tests_executed"],
            "promoted_at": _stamp(self.clock()),
            "source_run_id": run_id,
        }
        with self.database.lock:
            self.database.db.execute(
                "INSERT INTO belief_skills VALUES(?,?,?,?)",
                (run_id, name, skill["version_id"], _canonical(skill)),
            )
        self.database.audit(
            run_id,
            "skill_promoted",
            name=name,
            version_id=skill["version_id"],
            source_hash=digest_source,
            tests_hash=digest_tests,
            test_evidence_ref=test_evidence_ref,
        )
        return skill

    def skills(self, run_id: str) -> list[dict]:
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT payload FROM belief_skills WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        heads = {}
        for row in rows:
            skill = json.loads(row[0])
            heads[skill["name"]] = skill
        return [heads[name] for name in sorted(heads)]

    def export_bundle(self, run_id: str, *, tuple_: dict | None = None, _continuity=False) -> dict:
        beliefs = self.beliefs(run_id)
        events = beliefs._records()
        heads = self.fold(run_id)
        selected = {
            record["id"]
            for record in heads
            if _continuity
            or (
                record["status"] == "held"
                and (record["rank"] >= 3 or record["kind"] in {"strategy", "self"})
            )
        }
        deltas = []
        previous = "sha256:" + "0" * 64
        for event in events:
            if event["belief"]["id"] in selected:
                delta = {
                    "belief": copy.deepcopy(event["belief"])
                    if _continuity
                    else _public(event["belief"]),
                    "source_hash": event["hash"],
                    "prev_hash": previous,
                }
                delta["hash"] = _hash(delta)
                previous = delta["hash"]
                deltas.append(delta)
        evidence_index = {}
        for record in [delta["belief"] for delta in deltas]:
            for ref in [item["ref"] for item in record["evidence"]] + [
                link["to"] for link in record["links"]
            ]:
                resolved = self._resolve_evidence(run_id, ref)
                if resolved is None:
                    related = next((item for item in heads if item["id"] == ref), None)
                    resolved = related and {
                        "recorded_at": related["recorded_at"],
                        "entity_type": "belief",
                    }
                if resolved:
                    evidence_index[ref] = {
                        "recorded_at": resolved.get("recorded_at", resolved.get("ts")),
                        "source_run_id": resolved.get("run_id", run_id),
                        "entity_type": resolved.get("entity_type", "observation"),
                    }
                    if resolved.get("evidence_origin") == "workspace_note":
                        evidence_index[ref].update(
                            {
                                key: resolved[key]
                                for key in (
                                    "evidence_origin",
                                    "class",
                                    "hash",
                                    "bytes",
                                    "receipt_hash",
                                    "content_parsed",
                                )
                                if key in resolved
                            }
                        )
        manifest = {
            "schema_id": SCHEMA_ID,
            "purpose": "continuity" if _continuity else "pattern_transfer",
            "source_run_id": run_id,
            "source_series_id": self._config(run_id).get("series_id"),
            "tuple": tuple_ if tuple_ is not None else self._config(run_id).get("tuple", {}),
            "belief_ids": sorted(selected),
            "head": previous,
            "key_id": self._key_id,
            "source_chain": beliefs.verify(),
            "kernel_chain": self.database.verify(run_id),
        }
        bundle = {
            "manifest": manifest,
            "deltas": deltas,
            "evidence_index": evidence_index,
            "skills": self.skills(run_id),
            "projection": [record for record in heads if record["id"] in selected],
            "observations": self._observations(run_id) if _continuity else [],
            "self_summary": [
                record["claim"]
                for record in heads
                if record["id"] in selected and record["kind"] == "self"
            ],
        }
        bundle["signature"] = hmac.new(
            self._bundle_key, _canonical(bundle).encode(), hashlib.sha256
        ).hexdigest()
        self.database.audit(
            run_id, "belief_bundle_export", bundle_hash=_hash(bundle), count=len(selected)
        )
        return bundle

    def export_continuity(self, run_id: str) -> dict:
        """Complete session memory; unlike pattern transfer, this drops no belief."""
        return self.export_bundle(run_id, _continuity=True)

    def import_continuity(self, run_id: str, bundle: dict) -> dict:
        if bundle.get("manifest", {}).get("purpose") != "continuity":
            raise BeliefValidationError("session continuity requires a complete continuity bundle")
        return self.import_bundle(run_id, bundle)

    def validate_bundle(self, bundle: dict, *, continuity=False) -> dict:
        """Validate an import completely before a caller creates its destination run."""
        try:
            supplied = copy.deepcopy(bundle)
            signature = supplied.pop("signature", None)
            manifest = supplied["manifest"]
            if manifest["schema_id"] != SCHEMA_ID:
                raise BeliefValidationError("unsupported belief bundle schema")
            if manifest.get("purpose") not in {"continuity", "pattern_transfer"}:
                raise BeliefValidationError("unknown belief import purpose")
            if continuity and manifest["purpose"] != "continuity":
                raise BeliefValidationError("complete continuity bundle is required")
            key_id = manifest["key_id"]
            key = (
                self._bundle_key
                if key_id == self._key_id
                else self._trusted_bundle_keys.get(key_id)
            )
            if (
                not isinstance(signature, str)
                or key is None
                or not hmac.compare_digest(
                    signature,
                    hmac.new(key, _canonical(supplied).encode(), hashlib.sha256).hexdigest(),
                )
            ):
                raise BeliefValidationError("belief bundle signature is not trusted")
            if (
                manifest["source_chain"].get("valid") is not True
                or manifest["kernel_chain"].get("valid") is not True
            ):
                raise BeliefIntegrityError("source belief or kernel chain was invalid")
            previous = "sha256:" + "0" * 64
            heads = {}
            last_time = None
            instant = _time(self.clock())
            for delta in supplied["deltas"]:
                content = {key: value for key, value in delta.items() if key != "hash"}
                if delta["prev_hash"] != previous or _hash(content) != delta["hash"]:
                    raise BeliefIntegrityError("belief delta hash chain failed")
                previous = delta["hash"]
                record = delta["belief"]
                stamp = _time(record["recorded_at"])
                if stamp > instant or (last_time is not None and stamp < last_time):
                    raise BeliefValidationError(
                        "belief bundle has future or non-monotonic system times"
                    )
                last_time = stamp
                if (
                    not isinstance(record["rank"], int)
                    or isinstance(record["rank"], bool)
                    or not 0 <= record["rank"] <= 5
                ):
                    raise BeliefValidationError("belief bundle rank is invalid")
                if not isinstance(record["claim"], str) or not 1 <= len(record["claim"]) <= 300:
                    raise BeliefValidationError("belief bundle claim is invalid")
                if record["kind"] not in {"fact", "rule", "value", "strategy", "self"} or record[
                    "status"
                ] not in {
                    "proposed",
                    "held",
                    "conditional",
                    "review",
                    "superseded",
                    "retracted",
                    "expired",
                }:
                    raise BeliefValidationError(
                        "belief bundle has an invalid kind or lifecycle state"
                    )
                heads[record["id"]] = record
            if previous != manifest["head"] or sorted(heads) != manifest["belief_ids"]:
                raise BeliefIntegrityError("belief bundle manifest does not match its fold")
            projection = {record["id"]: record for record in supplied["projection"]}
            if sorted(projection) != sorted(heads) or any(
                projection[belief_id]["rank"] != record["rank"]
                or projection[belief_id]["claim"] != record["claim"]
                for belief_id, record in heads.items()
            ):
                raise BeliefIntegrityError("belief projection changed a semantic claim or rank")
            for observation in supplied.get("observations", []):
                if (
                    _time(observation["observed_at"]) > _time(observation["recorded_at"])
                    or _time(observation["recorded_at"]) > instant
                ):
                    raise BeliefValidationError("belief bundle observation has invalid world times")
            names = set()
            for skill in supplied.get("skills", []):
                if skill["name"] in names or skill["tests_executed"] < 1:
                    raise BeliefValidationError("belief bundle has duplicate or untested skills")
                names.add(skill["name"])
                if (
                    "sha256:" + hashlib.sha256(skill["source"].encode()).hexdigest()
                    != skill["source_hash"]
                    or "sha256:" + hashlib.sha256(skill["tests"].encode()).hexdigest()
                    != skill["tests_hash"]
                ):
                    raise BeliefIntegrityError("skill byte hashes failed")
            return {
                "valid": True,
                "purpose": manifest["purpose"],
                "beliefs": len(heads),
                "source_run_id": manifest["source_run_id"],
                "bundle_hash": _hash(bundle),
            }
        except (KeyError, TypeError, AttributeError) as error:
            raise BeliefValidationError("malformed belief bundle") from error

    def import_bundle(self, run_id: str, bundle: dict) -> dict:
        self.validate_bundle(bundle)
        supplied = copy.deepcopy(bundle)
        signature = supplied.pop("signature", None)
        key_id = supplied.get("manifest", {}).get("key_id")
        key = self._bundle_key if key_id == self._key_id else self._trusted_bundle_keys.get(key_id)
        if (
            key is None
            or not isinstance(signature, str)
            or not hmac.compare_digest(
                signature,
                hmac.new(key, _canonical(supplied).encode(), hashlib.sha256).hexdigest(),
            )
        ):
            raise BeliefValidationError("pattern bundle signature is not trusted")
        if supplied["manifest"].get("schema_id") != SCHEMA_ID:
            raise BeliefValidationError("unsupported pattern bundle schema")
        previous = "sha256:" + "0" * 64
        heads = {}
        for delta in supplied.get("deltas", []):
            content = {key: value for key, value in delta.items() if key != "hash"}
            if delta.get("prev_hash") != previous or delta.get("hash") != _hash(content):
                raise BeliefIntegrityError("pattern delta hash chain failed")
            previous = delta["hash"]
            heads[delta["belief"]["id"]] = delta["belief"]
        if (
            previous != supplied["manifest"]["head"]
            or sorted(heads) != supplied["manifest"]["belief_ids"]
        ):
            raise BeliefIntegrityError("pattern manifest does not match fold")
        projection = {record["id"]: record for record in supplied.get("projection", [])}
        if sorted(projection) != sorted(heads) or any(
            projection[belief_id]["claim"] != snapshot["claim"]
            or projection[belief_id]["rank"] != snapshot["rank"]
            for belief_id, snapshot in heads.items()
        ):
            raise BeliefIntegrityError("pattern projection changed a semantic claim or rank")
        for skill in supplied.get("skills", []):
            if (
                "sha256:" + hashlib.sha256(skill["source"].encode()).hexdigest()
                != skill["source_hash"]
                or "sha256:" + hashlib.sha256(skill["tests"].encode()).hexdigest()
                != skill["tests_hash"]
            ):
                raise BeliefIntegrityError("skill content hashes do not match pattern bundle")
        beliefs = self.beliefs(run_id)
        if beliefs.fold():
            raise BeliefValidationError("pattern import requires a fresh belief store")
        with self.database.lock:
            for ref, metadata in supplied.get("evidence_index", {}).items():
                entry = {
                    **metadata,
                    "run_id": run_id,
                    "imported_from": supplied["manifest"]["source_run_id"],
                }
                self.database.db.execute(
                    "INSERT OR REPLACE INTO belief_imports VALUES(?,?,?)",
                    (run_id, ref, _canonical(entry)),
                )
        snapshots = {} if supplied["manifest"].get("purpose") == "continuity" else projection
        for belief_id, snapshot in sorted(snapshots.items()):

            def operation(events, state, stamp, record=snapshot):
                imported = copy.deepcopy(record)
                imported["imported_kernel"] = imported.pop("kernel", {})
                imported.update(
                    op="assert", recorded_at=stamp, version_id=identifier("bv"), flags=[]
                )
                imported.pop("supersedes_version", None)
                return imported

            beliefs._transact(operation)
        if supplied["manifest"].get("purpose") == "continuity":
            with beliefs._lock:
                source_clock = beliefs._clock
                try:
                    for delta in supplied["deltas"]:
                        snapshot = copy.deepcopy(delta["belief"])
                        if _time(snapshot["recorded_at"]) > _time(self.clock()):
                            raise BeliefValidationError("continuity contains future system times")
                        beliefs._clock = lambda stamp=snapshot["recorded_at"]: _time(stamp)
                        beliefs._transact(
                            lambda events, state, stamp, record=snapshot: copy.deepcopy(record)
                        )
                finally:
                    beliefs._clock = source_clock
            with self.database.lock:
                for observation in supplied.get("observations", []):
                    self.database.db.execute(
                        "INSERT INTO belief_observations VALUES(?,?,?,?,?)",
                        (
                            run_id,
                            observation["observation_ref"],
                            observation["observed_at"],
                            observation["recorded_at"],
                            _canonical(observation),
                        ),
                    )
        with self.database.lock:
            for skill in supplied.get("skills", []):
                self.database.db.execute(
                    "INSERT INTO belief_skills VALUES(?,?,?,?)",
                    (
                        run_id,
                        skill["name"],
                        skill["version_id"],
                        _canonical(skill),
                    ),
                )
        self.database.audit(
            run_id,
            "belief_bundle_import",
            source_run_id=supplied["manifest"]["source_run_id"],
            bundle_hash=_hash(bundle),
            count=len(heads),
        )
        self.render(run_id)
        return {
            "imported": len(heads),
            "source_run_id": supplied["manifest"]["source_run_id"],
            "manifest": supplied["manifest"],
        }

    async def recover(self):
        for run in self.store.list_runs():
            path = self.store.run_dir(run["run_id"]) / "beliefs" / "events.jsonl"
            if path.exists():
                if not self.beliefs(run["run_id"]).verify()["valid"]:
                    raise BeliefIntegrityError("belief recovery failed integrity verification")
                self.render(run["run_id"])

    async def aclose(self):
        self.database.close()
