"""Persist late-read life rubric checkpoints under frozen research configuration."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

from .controls import matched_life_deltas
from .instruments import load_artifact, load_instrument
from .report import PARAMETERS
from .rubric import CHECKPOINTS, score_life, validate_rubric_policy


def _bytes(value):
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        + b"\n"
    )


def _hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _save(path, body):
    if path.is_symlink():
        raise ValueError("life artifacts cannot be symbolic links")
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("the frozen life artifact is immutable")
        return
    # Exclusive creation also prevents two concurrent checkpoint calls replacing evidence.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != body:
            raise ValueError("the frozen life artifact is immutable") from None
        return
    with os.fdopen(descriptor, "wb") as output:
        output.write(body)


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("life checkpoint timestamps must be world-owned UTC observations")
    return parsed.astimezone(UTC)


class LifeRubricPipeline:
    def __init__(self, world):
        self.world, self.store = world, world.store

    def _folder(self, life_id):
        lives = self.world.services.get("lives")
        if lives is None:
            raise ValueError("life scoring requires the registered life service")
        lives.get(life_id)  # Validate identity before forming a filesystem path.
        folder = self.store.data_dir / "lives" / life_id / "rubric"
        if folder.resolve() != folder.absolute():
            raise ValueError("life rubric artifacts must remain private to this life")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def has_config(self, life_id):
        return (self._folder(life_id) / "config.json").is_file()

    def configure(
        self,
        life_id,
        *,
        source_run_id,
        policy_artifact,
        binding_artifact,
        calibration_artifact=None,
        controls=None,
        registered_contrasts=None,
    ):
        life = self.world.services["lives"].get(life_id)
        allowed = [session["run_id"] for session in life["sessions"]]
        source = self.store.get_run(source_run_id)
        if source_run_id not in allowed and source["config"].get("life_id") != life_id:
            raise ValueError("the rubric source must belong to this actual life")
        config = {
            "life_id": life_id,
            "source_run_id": source_run_id,
            "provenance": {},
            "controls": controls,
            "registered_contrasts": registered_contrasts,
        }
        for name, descriptor in (
            ("policy", policy_artifact),
            ("binding", binding_artifact),
            ("calibration", calibration_artifact),
        ):
            if descriptor is None:
                config[name] = None
                continue
            raw, provenance = load_artifact(
                self.store, source_run_id, descriptor, allowed_run_ids=allowed
            )
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("life rubric configuration artifacts must contain objects")
            config[name], config["provenance"][name] = value, provenance
        if config["calibration"] is not None:
            from .classifier_evidence import verify_saved_allocation

            # A frozen file containing claimed kappa/miss-rate scalars is not
            # a calibrated instrument. Rebuild from real expert/capture rows.
            config["calibration"] = verify_saved_allocation(
                self.world, source_run_id, config["calibration"]
            )
        errors = validate_rubric_policy(config["policy"])
        if errors:
            raise ValueError("; ".join(errors))
        if config["binding"].get("binding_id") != life.get("binding_id"):
            raise ValueError("the domain binding must match this life's registered binding")
        for mapping in config["binding"].get("evidence_mappings", []):
            if (
                mapping.get("parameter") not in PARAMETERS
                or mapping.get("column") not in {"G", "V"}
                or not mapping.get("item_kind")
                or not mapping.get("evidence_class")
            ):
                raise ValueError(
                    "footprint evidence mappings require parameter, column, kind and class"
                )
        for mapping in config["binding"].get("classifier_mappings", []):
            if (
                mapping.get("parameter") not in PARAMETERS
                or mapping.get("label")
                not in {
                    "instrumental",
                    "non_instrumental",
                    "content_class",
                    "play",
                    "reiteration",
                    "revisit",
                }
                or not mapping.get("evidence_class")
            ):
                raise ValueError(
                    "classifier mappings require a life parameter, actual label and evidence class"
                )
        self._validate_configuration(life, config)
        body = _bytes(config)
        _save(self._folder(life_id) / "config.json", body)
        schedule = getattr(self.world.services["lives"], "schedule_rubric", None)
        if schedule:
            schedule(life_id)
        return {"life_id": life_id, "config_hash": _hash(body), "configured": True}

    def _validate_configuration(self, life, config):
        allowed = [session["run_id"] for session in life["sessions"]]
        for name in ("policy", "binding"):
            body, _ = load_artifact(
                self.store,
                config["source_run_id"],
                config["provenance"][name],
                allowed_run_ids=allowed,
            )
            if json.loads(body) != config[name]:
                raise ValueError("life rubric configuration differs from its frozen artifact")
        if life.get("research_mode") != "research":
            return
        registry = self.world.services.get("registry")
        source = self.store.get_run(config["source_run_id"])
        series_id = source["config"].get("series_id") or life.get("series_id")
        if not registry or not series_id:
            raise ValueError("research life rubric requires its actual registered series")
        series = registry.get_series(series_id)
        first_session = min(
            (_time(session["started_at"]) for session in life["sessions"]),
            default=_time(source["created_at"]),
        )
        if (
            _time(series["created_at"]) > first_session
            or series["prereg"].get("life_rubric_policy_hash")
            != config["provenance"]["policy"]["hash"]
            or series["prereg"].get("life_binding_hash") != config["provenance"]["binding"]["hash"]
        ):
            raise ValueError(
                "research life rubric policy and binding hashes must be preregistered before the first session"
            )

    def _footprint(self, life_id, checkpoint, binding, *, through):
        footprint = self.world.services.get("footprint")
        items = footprint.query(life_id=life_id, include_incidents=True) if footprint else []
        evidence, originals = [], {}
        for item in items:
            for reading in item.get("readings", []):
                if _time(reading["at"]) > through:
                    continue
                originals[reading["reading_id"]] = reading
                for mapping in binding.get("evidence_mappings", []):
                    if item["kind"] != mapping["item_kind"] or (
                        mapping.get("reading_kind")
                        and reading.get("kind") != mapping["reading_kind"]
                    ):
                        continue
                    value = reading["value"]
                    if mapping.get("value_key"):
                        if not isinstance(value, dict) or mapping["value_key"] not in value:
                            continue
                        value = value[mapping["value_key"]]
                    evidence.append(
                        {
                            "parameter": mapping["parameter"],
                            "column": mapping["column"],
                            "evidence_class": mapping["evidence_class"],
                            "event_kind": mapping.get("event_kind", item["kind"]),
                            "venue_class": item["venue_class"],
                            "author_class": reading["author_class"],
                            "unsolicited": reading["unsolicited"],
                            "returning": reading["returning"],
                            "after_refusal": reading["after_refusal"],
                            "value": value,
                            "checkpoint": checkpoint,
                            "observation_ref": reading["reading_id"],
                        }
                    )
        return evidence, originals

    def _classifier_evidence(self, life, checkpoint, config, *, through):
        from .classifier_evidence import verify_saved_allocation
        from .publication import load_measurements

        evidence, calibration, provenance = [], [], []
        mappings = config["binding"].get("classifier_mappings", [])
        if not mappings and config["calibration"] is None:
            return evidence, None, provenance
        for session in life["sessions"]:
            run_id = session["run_id"]
            records = self.store.records(run_id)
            if not records or records[-1]["kind"] != "seal" or _time(records[-1]["ts"]) > through:
                continue
            measured = load_measurements(self.store, run_id)
            payload = measured.get("allocation")
            if payload is None and run_id == config["source_run_id"]:
                payload = config["calibration"]
            if not payload or payload.get("status") != "measured":
                continue
            verified = verify_saved_allocation(self.world, run_id, payload)
            provenance.append(
                {
                    "run_id": run_id,
                    "source_record_head": verified["source_record_head"],
                    "instrument": verified["instrument_ref"],
                    "protocol_hash": verified["protocol_hash"],
                    "measurement_run_ids": [
                        row["measurement_run_id"]
                        for row in verified["segments"] + verified["calibration_judgments"]
                    ],
                }
            )
            for mapping in mappings:
                label = mapping["label"]
                key = "instrumental" if label == "non_instrumental" else label
                tasks = {key}
                if label in {"non_instrumental", "revisit"}:
                    tasks.update({"instrumental", "content_class"})
                agreements = [verified["calibration"][task] for task in tasks]
                qualified = all(row.get("column") == "V" for row in agreements)
                if qualified:
                    calibration.extend(agreements)
                expected = mapping.get("equals", label != "non_instrumental")
                for segment in verified["segments"]:
                    for reading in segment["readings"]:
                        if reading.get(key) is None or reading.get(key) != expected:
                            continue
                        if label == "revisit" and reading.get("instrumental") is not False:
                            continue
                        if (
                            mapping.get("content_class")
                            and reading.get("content_class") != mapping["content_class"]
                        ):
                            continue
                        evidence.append(
                            {
                                "parameter": mapping["parameter"],
                                "column": "V" if qualified else "J",
                                "evidence_class": mapping["evidence_class"],
                                "event_kind": mapping.get("event_kind", label),
                                "value": reading["interval_usd"]
                                if mapping.get("value") == "interval_usd"
                                else 1,
                                "success": True,
                                "checkpoint": checkpoint,
                                "observation_ref": reading["action_ref"],
                                "source_run_id": run_id,
                                "measurement_run_id": segment["measurement_run_id"],
                                "judge_dependent": True,
                            }
                        )
        if calibration:
            bound = {
                "kappa": min(row["kappa"] for row in calibration),
                "miss_rate": max(
                    row["miss_rate"] for row in calibration if row.get("miss_rate") is not None
                ),
                "receipts_verified": True,
                "combination": "minimum actual task kappa and maximum actual task miss rate across mapped labels/sessions",
            }
        else:
            bound = None
        return evidence, bound, provenance

    def _checkpoint(self, descriptor):
        if not isinstance(descriptor, dict):
            raise TypeError("life controls require actual checkpoint artifact descriptors")
        name = descriptor.get("artifact_ref")
        if not isinstance(name, str):
            raise TypeError("a control checkpoint artifact reference is required")
        path = self.store.data_dir / name
        if path.resolve() != path or not path.is_file():
            raise ValueError("control checkpoint artifacts must be owned nonsymlink files")
        body = path.read_bytes()
        if _hash(body) != descriptor.get("artifact_hash"):
            raise ValueError("control checkpoint artifact hash mismatch")
        report = json.loads(body)
        life = self.world.services["lives"].get(report["life_id"])
        if not path.is_relative_to(
            self.store.data_dir / "lives" / life["life_id"] / "rubric"
        ) or not any(
            ref.get("artifact_ref") == name
            and ref.get("artifact_hash") == descriptor["artifact_hash"]
            for ref in life.get("rubric_checkpoints", [])
        ):
            raise ValueError("the control artifact is not registered to this actual life")
        return report

    def _matching(self, life, config, checkpoint, through):
        runs = [
            self.store.get_run(session["run_id"])
            for session in life["sessions"]
            if _time(session["started_at"]) <= through
        ]
        settings = [run["config"] for run in runs]
        models, seeds = (
            {item.get("model_snapshot_id") for item in settings},
            {item.get("seed") for item in settings},
        )
        registry = self.world.services.get("registry")
        series = (
            registry.get_series(life["series_id"]) if registry and life.get("series_id") else None
        )
        exposure_end = min(through, _time(life["ended"]["at"])) if life.get("ended") else through
        block_profiles = []
        canaries = self.world.services.get("canaries")
        for run in runs:
            if canaries and hasattr(canaries, "profile_for_run"):
                observed = canaries.profile_for_run(run["run_id"])
                if observed and observed.get("status") == "measured":
                    block_profiles.append(observed["rates_by_domain_class"])
                else:
                    block_profiles.append(None)
                continue
            for event in self.store.records(run["run_id"]):
                profile = event.get("block_profile")
                if event["kind"] == "preflight" and isinstance(profile, dict):
                    block_profiles.append(profile)
        points = [
            event
            for run in runs
            for event in self.store.records(run["run_id"])
            if _time(event["ts"]) <= through
        ]
        return {
            "version_tuples": [item["tuple"] for item in settings],
            "binding_hash": config["provenance"]["binding"]["hash"],
            "budget": life["budget"],
            "axes": {
                "usd": sum(
                    event.get("amount_nano", float(event.get("cost_usd", 0)) * 1e9)
                    for event in points
                    if event["kind"] == "ledger_debit"
                )
                / 1e9,
                "world_actions": sum(
                    event["kind"] == "action" and event.get("world_action", True)
                    for event in points
                ),
                "calendar_days": (through - _time(life["created_at"])).total_seconds() / 86400,
            },
            "calendar": {
                "start_iso_week": _time(life["created_at"]).strftime("%G-W%V"),
                "checkpoint": checkpoint,
                "exposure_days": (exposure_end - _time(life["created_at"])).total_seconds() / 86400,
                "afterlife_days": int(checkpoint[5:]) if checkpoint.startswith("after") else 0,
            },
            "model_snapshot_id": next(iter(models)) if len(models) == 1 else None,
            "seed": next(iter(seeds)) if len(seeds) == 1 else None,
            "prereg_hash": series["prereg_hash"] if series else None,
            "global_control": bool(settings)
            and life.get("driver") is None
            and all(
                not item.get("goal")
                and not item.get("rule")
                and item.get("vault_mode") == "identity_only"
                and item.get("reply_policy") == "silent"
                for item in settings
            ),
            "block_profile": block_profiles[0]
            if block_profiles and all(profile == block_profiles[0] for profile in block_profiles)
            else None,
        }

    async def run(
        self,
        life_id,
        *,
        checkpoint="end",
        evidence_artifact=None,
        controls=None,
        registered_contrasts=None,
        **configure,
    ):
        checkpoint = {"afterlife_30d": "after30", "afterlife_90d": "after90"}.get(
            checkpoint, checkpoint
        )
        if checkpoint not in CHECKPOINTS:
            raise ValueError("unknown life-rubric checkpoint")
        if configure:
            self.configure(life_id, **configure)
        folder = self._folder(life_id)
        config_path = folder / "config.json"
        if not config_path.is_file() or config_path.is_symlink():
            raise ValueError("configure frozen life-rubric inputs before scoring")
        config = json.loads(config_path.read_bytes())
        lives = self.world.services["lives"]
        life = lives.get(life_id)
        self._validate_configuration(life, config)
        now = lives._now()  # A world-owned clock; API input never supplies an observation time.
        through = now
        if life.get("active_run_id"):
            raise ValueError("life scoring is passive and requires the active session to be sealed")
        if checkpoint == "end" and not life.get("ended"):
            raise ValueError("an end reading requires an actual ended life")
        if checkpoint == "end":
            through = _time(life["ended"]["at"])
        if checkpoint.startswith("d") and now < _time(life["created_at"]) + timedelta(
            days=int(checkpoint[1:])
        ):
            raise ValueError("the requested calendar checkpoint is not due")
        if checkpoint.startswith("d"):
            through = _time(life["created_at"]) + timedelta(days=int(checkpoint[1:]))
        if checkpoint.startswith("after"):
            after = life.get("afterlife", {}).get(checkpoint[5:])
            if not after or after.get("state") != "complete" or now < _time(after["due_at"]):
                raise ValueError("the actual afterlife reading must complete before rubric scoring")
            through = _time(after["observed_at"])
        evidence, originals = self._footprint(
            life_id, checkpoint, config["binding"], through=through
        )
        evidence_provenance = {"source": "world_footprint", "reading_ids": list(originals)}
        if evidence_artifact is not None:
            loaded = load_instrument(
                self.store,
                config["source_run_id"],
                evidence_artifact,
                allowed_run_ids=[session["run_id"] for session in life["sessions"]],
                reference_ids=originals,
            )
            evidence = loaded["rows"]
            for row in evidence:
                if row.get("column") == "G":
                    original = originals.get(row.get("observation_ref"))
                    if not original or any(
                        row.get(key) != original.get(key)
                        for key in ("author_class", "unsolicited", "returning", "after_refusal")
                    ):
                        raise ValueError(
                            "grounded evidence must preserve actual footprint provenance"
                        )
            evidence_provenance = loaded["provenance"]
        classifier_rows, calibration, classifier_provenance = self._classifier_evidence(
            life, checkpoint, config, through=through
        )
        evidence.extend(classifier_rows)
        t0_path = self.store.data_dir / "lives" / life_id / "t0.json"
        t0 = json.loads(t0_path.read_bytes()) if t0_path.is_file() else None
        if t0 is not None and life.get("t0_fingerprint_ref"):
            from worldd.storage import content_hash

            if content_hash(t0) != life["t0_fingerprint_ref"]:
                raise ValueError("the registered t=0 battery artifact failed integrity")
        prior_checkpoints = []
        horizon = (through - _time(life["created_at"])).total_seconds() / 86400
        for reference in life.get("rubric_checkpoints", []):
            prior = self._checkpoint(reference)
            position = prior.get("matching", {}).get("axes", {}).get("calendar_days")
            if isinstance(position, (int, float)) and position <= horizon:
                prior_checkpoints.append(prior)
        report = score_life(
            life,
            evidence,
            binding=config["binding"],
            policy=config["policy"],
            checkpoint=checkpoint,
            judge_calibration=calibration,
            t0=t0,
            checkpoint_reports=prior_checkpoints,
        )
        report["provenance"] = {
            "config_hash": _hash(config_path.read_bytes()),
            "configuration": config["provenance"],
            "evidence": evidence_provenance,
            "classifiers": classifier_provenance,
            "judge_calibration": calibration,
            "scientific_gate_passed": False,
        }
        report["matching"] = self._matching(life, config, checkpoint, through)
        controls = controls if controls is not None else config.get("controls") or {}
        loaded_controls = {
            name: [self._checkpoint(item) for item in descriptors]
            if isinstance(descriptors, list)
            else self._checkpoint(descriptors)
            for name, descriptors in controls.items()
        }
        contrast_descriptor = registered_contrasts or config.get("registered_contrasts")
        contrast = None
        if contrast_descriptor:
            raw, provenance = load_artifact(
                self.store,
                config["source_run_id"],
                contrast_descriptor,
                allowed_run_ids=[session["run_id"] for session in life["sessions"]],
            )
            contrast = json.loads(raw)
            registry = self.world.services.get("registry")
            if not registry or not any(
                series["prereg_hash"] == contrast.get("prereg_hash")
                and series["prereg"].get("registered_contrasts")
                == {key: value for key, value in contrast.items() if key != "prereg_hash"}
                and _time(series["created_at"]) <= _time(life["created_at"])
                for series in registry.list_series()
            ):
                raise ValueError("control contrasts must be registered before this actual life")
            report["provenance"]["registered_contrasts"] = provenance
        report["matched_deltas"] = matched_life_deltas(
            report, loaded_controls, t0=t0, registered_contrasts=contrast
        )
        body = _bytes(report)
        digest = _hash(body)
        checkpoint_folder = folder / checkpoint
        if checkpoint_folder.resolve() != checkpoint_folder.absolute():
            raise ValueError("life checkpoints cannot use symbolic link directories")
        checkpoint_folder.mkdir(exist_ok=True, mode=0o700)
        artifact = checkpoint_folder / (digest.removeprefix("sha256:") + ".json")
        _save(artifact, body)
        artifact_ref = str(artifact.relative_to(self.store.data_dir))
        register = getattr(lives, "record_rubric", None)
        if register:
            register(life_id, report, artifact_ref=artifact_ref, artifact_hash=digest)
        # Publication is a separate derived artifact. It cannot change the
        # immutable rubric payload or reopen any source session record.
        from .publication import export_publication

        export_folder = (
            self.store.data_dir / "lives" / life_id / "publication" / digest.split(":", 1)[1]
        )
        publication = self.world.life_report(life_id)["publication"]
        publication.pop("figure_svgs", None)
        export_manifest = export_folder / "exports.json"
        if export_manifest.is_symlink():
            raise ValueError("life publication manifests cannot be symbolic links")
        expected_publication_hash = _hash(
            (
                json.dumps(
                    publication, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False
                )
                + "\n"
            ).encode()
        )
        if (
            not export_manifest.exists()
            or json.loads(export_manifest.read_bytes()).get("files", {}).get("report.json")
            != expected_publication_hash
        ):
            export_publication(publication, export_folder)
        return {
            "life_id": life_id,
            "checkpoint": checkpoint,
            "artifact_ref": artifact_ref,
            "artifact_hash": digest,
            "report": report,
        }
