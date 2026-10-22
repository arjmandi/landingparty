"""Immutable experimental registration, comparability, and evidence-backed gates."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from typing import Any

from .storage import canonical_json, content_hash, utc_now

VERSION_FIELDS = (
    "banner_policy_id",
    "forecast_policy_id",
    "runtime_protocol_id",
    "life_protocol_id",
    "belief_mount_policy_id",
    "world_id",
    "archive_id",
    "tool_set_id",
    "policy_id",
    "price_table_id",
    "breaker_policy_id",
    "search_provider_id",
    "extractor_id",
    "judge_id",
    "observer_id",
    "classifier_id",
    "block_profile_id",
    "harness_id",
    "model_snapshot_id",
    "model_value_document_id",
    "belief_schema_id",
    "kernel_policy_id",
    "belief_mount_policy_id",
    "slot_suggester_policy_id",
    "validator_policy_id",
    "trigger_policy_id",
    "salience_policy_id",
    "reflection_policy",
    "ledger_view_policy",
    "vault_mode",
    "console_steering",
    "human_time_priced",
    "wake_policy",
    "continuity",
    "browser_identity",
    "captcha_policy",
    "permeability",
    "binding_id",
    "rubric_disclosed",
    "aggregate_policy_id",
    "preamble_hash",
    "canary_policy_id",
    "market_policy_id",
    "perception_price_table_id",
)
STAGE_GATES = {
    0: (),
    1: ("shakedown",),
    2: ("shakedown", "R1"),
    3: ("shakedown", "R1", "R5", "L4", "IRB"),
}


class GateRequired(ValueError):
    """Required research evidence has not been registered or has not passed."""


class RegistryService:
    def __init__(self, world):
        self.world = world
        self.store = world.store
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_series (
                    series_id TEXT PRIMARY KEY, prereg_hash TEXT NOT NULL,
                    tuple_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS series_runs (
                    series_id TEXT NOT NULL REFERENCES research_series(series_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id), cell TEXT NOT NULL,
                    started_at TEXT NOT NULL, PRIMARY KEY(series_id,run_id)
                );
                CREATE TABLE IF NOT EXISTS research_gates (
                    gate_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
                    registered_at TEXT NOT NULL, eligible INTEGER NOT NULL,
                    positive INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registered_versions (
                    version_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gate_revocations (
                    gate_id TEXT PRIMARY KEY REFERENCES research_gates(gate_id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registered_executions (
                    study_id TEXT NOT NULL,seq INTEGER NOT NULL,run_id TEXT NOT NULL REFERENCES runs(run_id),
                    payload TEXT NOT NULL,PRIMARY KEY(study_id,seq),UNIQUE(study_id,run_id)
                );
                """
            )

    @staticmethod
    def tuple_id(version: dict[str, Any]) -> str:
        missing = [key for key in VERSION_FIELDS if key not in version]
        if missing:
            raise ValueError("Incomplete version tuple: " + ", ".join(missing))
        return content_hash({key: version[key] for key in VERSION_FIELDS})

    def register_version(self, kind: str, value: dict[str, Any]) -> dict[str, Any]:
        if not kind or len(kind) > 100:
            raise ValueError("A version kind is required")
        version_id = content_hash({"kind": kind, "value": value})
        result = {"version_id": version_id, "kind": kind, "value": value, "created_at": utc_now()}
        with self.store._transaction():
            self.store.db.execute(
                "INSERT OR IGNORE INTO registered_versions VALUES(?,?,?,?)",
                (version_id, kind, canonical_json(result), result["created_at"]),
            )
            row = self.store.db.execute(
                "SELECT payload FROM registered_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        return json.loads(row[0])

    def register_series(self, prereg: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
        from analysis.experiments import validate_prereg

        verdict = validate_prereg(prereg)
        if not verdict["valid"]:
            raise ValueError("Invalid pre-registration: " + "; ".join(verdict["errors"]))
        tuple_id = self.tuple_id(version)
        prereg_hash = verdict["prereg_hash"]
        series_id = "s_" + content_hash({"prereg_hash": prereg_hash, "tuple_id": tuple_id})[7:31]
        result = {
            "series_id": series_id,
            "prereg_hash": prereg_hash,
            "tuple_id": tuple_id,
            "tuple": version,
            "prereg": prereg,
            "created_at": utc_now(),
            "runs": [],
        }
        with self.store._transaction():
            self.store.db.execute(
                "INSERT OR IGNORE INTO research_series VALUES(?,?,?,?,?)",
                (series_id, prereg_hash, tuple_id, canonical_json(result), result["created_at"]),
            )
        return self.get_series(series_id)

    def get_version(self, version_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT payload FROM registered_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return json.loads(row[0])

    def get_series(self, series_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT payload FROM research_series WHERE series_id=?", (series_id,)
            ).fetchone()
            if row is None:
                raise KeyError(series_id)
            result = json.loads(row[0])
            result["runs"] = [
                dict(item)
                for item in self.store.db.execute(
                    "SELECT run_id,cell,started_at FROM series_runs WHERE series_id=? "
                    "ORDER BY started_at,run_id",
                    (series_id,),
                )
            ]
            return result

    def list_series(self) -> list[dict[str, Any]]:
        with self.store._lock:
            ids = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT series_id FROM research_series ORDER BY created_at"
                )
            ]
        return [self.get_series(series_id) for series_id in ids]

    def record_run(self, series_id: str, run_id: str, *, cell: str | dict = "default") -> dict:
        run = self.store.get_run(run_id)
        self.validate_run(series_id, run["config"])
        cell_name = canonical_json(cell) if isinstance(cell, dict) else str(cell)
        with self.store._transaction():
            self.store.db.execute(
                "INSERT OR IGNORE INTO series_runs VALUES(?,?,?,?)",
                (series_id, run_id, cell_name, run["created_at"]),
            )
        return self.get_series(series_id)

    def validate_run(self, series_id: str, config: dict) -> dict:
        """Validate a prospective tuple before creating an undeletable run row."""
        series = self.get_series(series_id)
        version = config.get("tuple", config)
        mismatches = [
            key
            for key in VERSION_FIELDS
            if key != "archive_id" and (key not in version or version[key] != series["tuple"][key])
        ]
        if mismatches:
            raise ValueError("Series refuses changed version tuple: " + ", ".join(mismatches))
        declared = config.get("series_id")
        if declared is not None and declared != series_id:
            raise ValueError("Run belongs to another registered series")
        return {"valid": True, "series_id": series_id, "tuple_id": series["tuple_id"]}

    def record_execution(self, study_id, run_id, *, cell, seed):
        """Bind an actual started row to its frozen plan before executing its harness."""
        with self.store._transaction():
            row = self.store.db.execute(
                "SELECT payload FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
            if row is None:
                raise KeyError(study_id)
            study = json.loads(row[0])
            matches = [item for item in study["cells"] if item["cell_id"] == cell]
            if study["state"] != "running" or run_id not in study["run_ids"] or len(matches) != 1:
                raise ValueError(
                    "The actual started run does not belong to this running frozen plan"
                )
            planned = matches[0]
            run = self.store.get_run(run_id)
            config = run["config"]
            expected, series_id, sources = planned["config"], planned["series_id"], {}
            if study.get("dependency_protocol"):
                bindings = [
                    item for item in study.get("execution_bindings", []) if item["run_id"] == run_id
                ]
                if len(bindings) != 1:
                    raise ValueError("A dependent execution requires its durable actual resolution")
                binding = bindings[0]
                payload = {
                    key: binding[key]
                    for key in (
                        "cell_id",
                        "seed",
                        "episode",
                        "sources",
                        "config",
                        "dependency_protocol_hash",
                    )
                }
                if (
                    binding["hash"] != content_hash(payload)
                    or binding["cell_id"] != cell
                    or binding["seed"] != seed
                    or binding["dependency_protocol_hash"] != study["dependency_protocol"]["hash"]
                ):
                    raise ValueError("The dependent execution resolution changed")
                expected, series_id, sources = (
                    binding["config"],
                    binding["series_id"],
                    binding["sources"],
                )
            if (
                seed not in planned["seeds"]
                or config.get("seed") != seed
                or any(
                    config.get(key) != expected.get(key)
                    for key in (
                        "goal",
                        "budget",
                        "packs",
                        "harness_id",
                        "model_snapshot_id",
                        "mode",
                        "archive_in",
                        "iota",
                        "continuity_source_run_id",
                    )
                )
                or config.get("series_id") != series_id
            ):
                raise ValueError("Actual execution differs from its frozen study cell")
            series = self.get_series(series_id)
            if series["prereg_hash"] != study["prereg_hash"]:
                raise ValueError("The execution uses another pre-registration")
            self.validate_run(series_id, config)
            previous = self.store.db.execute(
                "SELECT payload FROM registered_executions WHERE study_id=? AND run_id=?",
                (study_id, run_id),
            ).fetchone()
            if previous:
                value = json.loads(previous[0])
                if value["cell"] != cell or value["seed"] != seed:
                    raise ValueError("A started execution cannot be reassigned")
                return value
            seq = self.store.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM registered_executions WHERE study_id=?",
                (study_id,),
            ).fetchone()[0]
            value = {
                "study_id": study_id,
                "seq": seq,
                "run_id": run_id,
                "cell": cell,
                "seed": seed,
                "series_id": series_id,
                "prereg_hash": study["prereg_hash"],
                "started_at": run["created_at"],
                "episode": planned.get("episode", 1),
                "condition_id": planned.get("condition_id", cell),
                "dependencies": sources,
                "dependency_protocol_hash": (study.get("dependency_protocol") or {}).get("hash"),
            }
            self.store.db.execute(
                "INSERT INTO registered_executions VALUES(?,?,?,?)",
                (study_id, seq, run_id, canonical_json(value)),
            )
            self.store.db.execute(
                "UPDATE series_runs SET cell=? WHERE series_id=? AND run_id=?",
                (cell, series_id, run_id),
            )
            return value

    def execution_log(self, study_id):
        with self.store._lock:
            return [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM registered_executions WHERE study_id=? ORDER BY seq",
                    (study_id,),
                )
            ]

    def record_life_execution(self, study_id, run_id, runner_id):
        """A session is a nested source row, never an independent life replicate."""
        with self.store._transaction():
            study = json.loads(
                self.store.db.execute(
                    "SELECT payload FROM studies WHERE study_id=?", (study_id,)
                ).fetchone()[0]
            )
            runner = json.loads(
                self.store.db.execute(
                    "SELECT payload FROM life_runners WHERE runner_id=?", (runner_id,)
                ).fetchone()[0]
            )
            binding = runner.get("study_binding", {})
            run = self.store.get_run(run_id)
            tag = run["config"].get("execution_binding", {})
            cells = [cell for cell in study["cells"] if cell["cell_id"] == binding.get("cell_id")]
            intents = [
                intent
                for intent in runner.get("session_intents", [])
                if intent["hash"] == tag.get("resolution_hash")
            ]
            life = json.loads(
                self.store.db.execute(
                    "SELECT payload FROM lives WHERE life_id=?", (runner["life_id"],)
                ).fetchone()[0]
            )
            if (
                study["execution_kind"] != "lives"
                or run_id not in study["run_ids"]
                or study["state"] not in {"running", "paused", "completed", "failed"}
                or len(cells) != 1
                or len(intents) != 1
                or binding.get("study_id") != study_id
                or tag.get("study_id") != study_id
                or tag.get("cell_id") != binding["cell_id"]
                or tag.get("seed") != binding["seed"]
                or run["config"]["seed"] != binding["seed"]
                or cells[0]["config"]["life_id"] != life["life_id"]
                or run["config"]["life_id"] != life["life_id"]
                or not any(session["run_id"] == run_id for session in life["sessions"])
                or runner["configuration_hash"] != content_hash(runner["cell"])
                or intents[0]["hash"]
                != content_hash({key: value for key, value in intents[0].items() if key != "hash"})
            ):
                raise ValueError(
                    "The actual life session does not match its authorized frozen study worker"
                )
            series_id = run["config"]["series_id"]
            if self.get_series(series_id)["prereg_hash"] != study["prereg_hash"]:
                raise ValueError("The life session uses another pre-registration")
            self.validate_run(series_id, run["config"])
            previous = self.store.db.execute(
                "SELECT payload FROM registered_executions WHERE study_id=? AND run_id=?",
                (study_id, run_id),
            ).fetchone()
            if previous:
                return json.loads(previous[0])
            seq = self.store.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM registered_executions WHERE study_id=?",
                (study_id,),
            ).fetchone()[0]
            value = {
                "study_id": study_id,
                "seq": seq,
                "run_id": run_id,
                "cell": binding["cell_id"],
                "condition_id": cells[0].get("condition_id", binding["cell_id"]),
                "seed": binding["seed"],
                "life_id": life["life_id"],
                "runner_id": runner_id,
                "unit": "life_session",
                "episode": tag["episode"],
                "series_id": series_id,
                "prereg_hash": study["prereg_hash"],
                "started_at": run["created_at"],
                "dependencies": runner.get("dependency_resolution", {}).get("sources", {}),
                "dependency_protocol_hash": (study.get("dependency_protocol") or {}).get("hash"),
            }
            self.store.db.execute(
                "INSERT INTO registered_executions VALUES(?,?,?,?)",
                (study_id, seq, run_id, canonical_json(value)),
            )
            self.store.db.execute(
                "UPDATE series_runs SET cell=? WHERE series_id=? AND run_id=?",
                (binding["cell_id"], series_id, run_id),
            )
            return value

    @staticmethod
    def _interleaved(cells):
        remaining = Counter(cells)
        pending = set(remaining)
        for cell in cells:
            if cell not in pending:
                return False
            pending.remove(cell)
            remaining[cell] -= 1
            if not pending:
                pending = {name for name, count in remaining.items() if count > 0}
        return not pending

    def comparability(self, left: str | dict, right: str | dict, *, series_id: str | None = None):
        a = self.store.get_run(left) if isinstance(left, str) else left
        b = self.store.get_run(right) if isinstance(right, str) else right
        ca, cb = a["config"], b["config"]
        va, vb = ca.get("tuple", {}), cb.get("tuple", {})
        reasons = []
        for key in VERSION_FIELDS:
            if key == "archive_id":
                continue
            if key not in va or key not in vb:
                reasons.append("missing tuple element: " + key)
            elif va[key] != vb[key]:
                reasons.append("different tuple element: " + key)
        if ca.get("goal") != cb.get("goal") or ca.get("family") != cb.get("family"):
            reasons.append("different goal slice")
        pa, pb = ca.get("block_profile"), cb.get("block_profile")
        if not isinstance(pa, dict) or not isinstance(pb, dict) or not pa or set(pa) != set(pb):
            reasons.append("block profile is missing or has different domain classes")
        elif any(
            isinstance(pa[key], bool)
            or isinstance(pb[key], bool)
            or not isinstance(pa[key], (int, float))
            or not isinstance(pb[key], (int, float))
            or not math.isfinite(pa[key])
            or not math.isfinite(pb[key])
            or not 0 <= pa[key] <= 1
            or not 0 <= pb[key] <= 1
            or abs(pa[key] - pb[key]) > 0.05
            for key in pa
        ):
            reasons.append("block profile differs by more than five percentage points")
        same_archive = va.get("archive_id") is not None and va.get("archive_id") == vb.get(
            "archive_id"
        )
        if not same_archive:
            if ca.get("mode") != "live" or cb.get("mode") != "live":
                reasons.append("different archives without paired live conditions")
            wa = datetime.fromisoformat(a["created_at"]).isocalendar()[:2]
            wb = datetime.fromisoformat(b["created_at"]).isocalendar()[:2]
            if wa != wb:
                reasons.append("live runs are from different ISO weeks")
            if series_id is None:
                reasons.append("live interleaving has not been demonstrated")
            else:
                runs = self.get_series(series_id)["runs"]
                with self.store._lock:
                    cohorts = [
                        row[0]
                        for row in self.store.db.execute(
                            "SELECT DISTINCT study_id FROM registered_executions WHERE run_id=?",
                            (a["run_id"],),
                        )
                    ]
                for cohort in cohorts:
                    log = self.execution_log(cohort)
                    if any(item["run_id"] == b["run_id"] for item in log) and all(
                        item["prereg_hash"] == self.get_series(series_id)["prereg_hash"]
                        for item in log
                    ):
                        runs = log
                        break
                selected = [item for item in runs if item["run_id"] in {a["run_id"], b["run_id"]}]
                cells = [item["cell"] for item in runs]
                if len(selected) != 2 or len(set(cells)) < 2:
                    reasons.append("registered execution has no interleaved condition contrast")
                elif not self._interleaved(cells):
                    reasons.append("conditions ran in batches rather than interleaved order")
        return {
            "comparable": not reasons,
            "reasons": reasons,
            "left": a["run_id"],
            "right": b["run_id"],
        }

    def register_gate(self, name: str, report: dict, *, provenance: str = "research") -> dict:
        from analysis.experiments import validate_gate_report

        if name not in {"shakedown", "R1", "R5", "L4", "IRB"}:
            raise ValueError("Unknown stage gate")
        errors = list(validate_gate_report(name, report))
        if name == "shakedown":
            from analysis.programme import PilotReadiness

            errors.extend(PilotReadiness(self.world).verify(report))
        if name == "R1":
            from analysis.r1 import R1Readiness

            errors.extend(R1Readiness(self.world).verify(report))
        scientific = report.get("scientific", report.get("eligible_scientific", False)) is True
        if provenance != "research" or not scientific:
            errors.append("development/test evidence cannot authorize a research stage")
        run_ids = report.get("run_ids", [])
        artifact_hashes = {}
        if len(run_ids) != len(set(run_ids)):
            errors.append("duplicate run identities do not constitute independent samples")
        prereg_hash = report.get("prereg_hash", report.get("prereg_id"))
        series = [item for item in self.list_series() if item["prereg_hash"] == prereg_hash]
        actual_models: dict[str, set] = {}
        if name != "IRB" and not series:
            errors.append("pre-registration was not registered before the study")
        for run_id in run_ids:
            try:
                run = self.store.get_run(run_id)
            except (KeyError, ValueError):
                errors.append("unknown evidence run: " + str(run_id))
                continue
            if run["state"] not in {"sealed", "killed", "ended"} or run.get("tainted"):
                errors.append("evidence run is unsealed or tainted: " + run_id)
            if run["config"].get("research_mode") != "research":
                errors.append("evidence run is not a research run: " + run_id)
            model = run["config"].get("model_snapshot_id")
            if model:
                actual_models.setdefault(model, set()).add(run["config"].get("seed"))
            matching = [item for item in series if any(r["run_id"] == run_id for r in item["runs"])]
            if not matching and name != "IRB":
                errors.append("evidence run is outside the registered series: " + run_id)
            if matching:
                earliest = min(datetime.fromisoformat(item["created_at"]) for item in matching)
                if earliest > datetime.fromisoformat(run["created_at"]):
                    errors.append("pre-registration postdates evidence run: " + run_id)
            if (
                not self.store.verify_record(run_id)["valid"]
                or not self.store.reconcile(run_id)["valid"]
            ):
                errors.append("evidence run fails record/ledger integrity: " + run_id)
            events = self.store.records(run_id)
            if not events or events[-1]["kind"] != "seal":
                errors.append("evidence run has no terminal seal: " + run_id)
            artifact = self.store.run_dir(run_id) / "report.json"
            if not artifact.is_file():
                errors.append("evidence run has no saved report artifact: " + run_id)
            else:
                try:
                    artifact_hashes[run_id] = content_hash(json.loads(artifact.read_text("utf-8")))
                except (OSError, ValueError):
                    errors.append("evidence report artifact is invalid: " + run_id)
        positive = report.get("positive", report.get("verdict") == "positive") is True
        if name == "R1" and not positive:
            errors.append("R1 did not report a positive formation result")
        if name == "R1":
            reported_models = set(report.get("by_model", {}))
            if reported_models != set(actual_models):
                errors.append("R1 model reports do not match the actual model snapshots")
            if any(len(seeds) < 5 for seeds in actual_models.values()):
                errors.append("R1 actual runs do not cover five seeds per model")
        gate_id = (
            "gate_" + content_hash({"name": name, "report": report, "provenance": provenance})[7:31]
        )
        result = {
            "gate_id": gate_id,
            "name": name,
            "report": report,
            "provenance": provenance,
            "registered_at": utc_now(),
            "eligible": not errors,
            "positive": positive,
            "errors": sorted(set(errors)),
            "artifact_hashes": artifact_hashes,
        }
        with self.store._transaction():
            self.store.db.execute(
                "INSERT OR IGNORE INTO research_gates VALUES(?,?,?,?,?,?)",
                (
                    gate_id,
                    name,
                    canonical_json(result),
                    result["registered_at"],
                    int(result["eligible"]),
                    int(positive),
                ),
            )
            row = self.store.db.execute(
                "SELECT payload FROM research_gates WHERE gate_id=?", (gate_id,)
            ).fetchone()
        return json.loads(row[0])

    def gates(self) -> list[dict]:
        with self.store._lock:
            gates = [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM research_gates ORDER BY registered_at"
                )
            ]
            for gate in gates:
                row = self.store.db.execute(
                    "SELECT payload FROM gate_revocations WHERE gate_id=?", (gate["gate_id"],)
                ).fetchone()
                gate["revocation"] = json.loads(row[0]) if row else None
            return gates

    def revoke_gate(self, gate_id: str, *, reason: str, actor: str = "researcher") -> dict:
        """Permanently withdraw one immutable approval without rewriting its evidence."""
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(actor, str)
            or not actor.strip()
        ):
            raise ValueError("A gate revocation requires an accountable actor and reason")
        with self.store._transaction():
            if not self.store.db.execute(
                "SELECT 1 FROM research_gates WHERE gate_id=?", (gate_id,)
            ).fetchone():
                raise KeyError(gate_id)
            value = {
                "gate_id": gate_id,
                "revoked": True,
                "revoked_at": utc_now(),
                "reason": reason,
                "actor": actor,
            }
            value["hash"] = content_hash(value)
            self.store.db.execute(
                "INSERT OR IGNORE INTO gate_revocations VALUES(?,?)",
                (gate_id, canonical_json(value)),
            )
            return json.loads(
                self.store.db.execute(
                    "SELECT payload FROM gate_revocations WHERE gate_id=?", (gate_id,)
                ).fetchone()[0]
            )

    def require_stage(self, stage: int, research_mode: str = "research") -> dict:
        if stage not in STAGE_GATES:
            raise ValueError("Stage must be between zero and three")
        if research_mode not in {"research", "development"}:
            raise ValueError("Unknown research mode")
        required = STAGE_GATES[stage]
        accepted = {
            gate["name"]: gate
            for gate in self.gates()
            if gate["eligible"] and self._gate_still_valid(gate)
        }
        missing = [name for name in required if name not in accepted]
        if research_mode == "development":
            return {
                "stage": stage,
                "research_mode": research_mode,
                "research_authorized": False,
                "development_only": True,
                "missing_gates": missing,
                "external_writes_allowed": False,
            }
        if missing:
            raise GateRequired("Research stage requires registered evidence: " + ", ".join(missing))
        return {
            "stage": stage,
            "research_mode": research_mode,
            "research_authorized": True,
            "development_only": False,
            "gate_ids": [accepted[name]["gate_id"] for name in required],
        }

    def _gate_still_valid(self, gate: dict) -> bool:
        with self.store._lock:
            if self.store.db.execute(
                "SELECT 1 FROM gate_revocations WHERE gate_id=?", (gate["gate_id"],)
            ).fetchone():
                return False
        if gate["name"] == "shakedown":
            from analysis.programme import PilotReadiness

            if PilotReadiness(self.world).verify(gate["report"]):
                return False
        if gate["name"] == "R1":
            from analysis.r1 import R1Readiness

            if R1Readiness(self.world).verify(gate["report"]):
                return False
        if gate["name"] == "IRB":
            report = gate["report"]
            if report.get("revoked") is True or report.get("revoked_at"):
                return False
            if report.get("expires_at"):
                try:
                    expiry = datetime.fromisoformat(report["expires_at"])
                    if expiry.tzinfo is None or expiry <= datetime.now().astimezone():
                        return False
                except (ValueError, TypeError):
                    return False
        for run_id, expected in gate.get("artifact_hashes", {}).items():
            try:
                run = self.store.get_run(run_id)
                artifact = self.store.run_dir(run_id) / "report.json"
                if run.get("tainted") or run["state"] not in {"sealed", "killed", "ended"}:
                    return False
                if content_hash(json.loads(artifact.read_text("utf-8"))) != expected:
                    return False
                if (
                    not self.store.verify_record(run_id)["valid"]
                    or not self.store.reconcile(run_id)["valid"]
                ):
                    return False
                events = self.store.records(run_id)
                if not events or events[-1]["kind"] != "seal":
                    return False
            except (OSError, KeyError, ValueError):
                return False
        return True

    async def recover(self):
        return None

    async def aclose(self):
        return None
