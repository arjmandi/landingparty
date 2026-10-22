"""Passive revealed signatures and collapse, separate from the frozen F7 ICC vector."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections import Counter
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urldefrag

from worldd.util import canonical, digest
from worldd.workspaces import FILE_FLAGS, _directory, snapshot_manifest

from .judges import JudgeSpec, _json_reply
from .metrics import jensen_shannon
from .report import unmeasured
from .trajectory import trajectory_metrics
from .values import _receipt

POLICY_ID = "verified-revealed-signature-collapse@1"
LEISURE_POLICY_ID = "leisure-class-duration-profile@1"
ACTIVE = {"held", "review", "conditional", "proposed"}
THIRD_PARTY = {"independent_human", "independent_agent", "arena_participant"}
KNOWN_AUTHORS = THIRD_PARTY | {"sponsor", "sibling", "probe", "agent", "reflector", "world"}


def _source(world, run_id):
    run, rows = world.store.get_run(run_id), world.store.records(run_id)
    ledger = world.store.reconcile(run_id)
    if (
        run["state"] != "sealed"
        or run.get("tainted")
        or not rows
        or rows[-1]["kind"] != "seal"
        or not world.store.verify_record(run_id)["valid"]
        or not ledger["valid"]
        or ledger.get("reserved_usd", 0)
        or rows[0].get("config_hash") != digest(run["config"])
    ):
        raise ValueError("Signature details require an immutable sealed and reconciled source")
    return run, rows


def _date(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _seconds(start, stop):
    left, right = _date(start), _date(stop)
    return (right - left).total_seconds() if left and right and right >= left else None


def _read_file(root, relative, expected, *, limit=None):
    """Read only an anchored regular file through no-follow directory descriptors."""
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Signature file escapes its source")
    with _directory(root / str(path.parent)) as parent:
        descriptor = os.open(path.name, FILE_FLAGS, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or limit is not None and info.st_size > limit:
                raise ValueError("Signature file is not a bounded regular file")
            chunks = []
            while block := os.read(descriptor, 1024 * 1024):
                chunks.append(block)
                if limit is not None and sum(map(len, chunks)) > limit:
                    raise ValueError("Signature file exceeds its declared bound")
            body = b"".join(chunks)
            after = os.fstat(descriptor)
            if any(
                getattr(info, key) != getattr(after, key)
                for key in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ValueError("Signature file changed while reading")
        finally:
            os.close(descriptor)
    if digest(body) != expected:
        raise ValueError("Signature file failed its recorded content hash")
    return body


def _snapshot(world, run_id):
    _, rows = _source(world, run_id)
    anchors = [row for row in rows if row["kind"] == "workspace_snapshot"]
    if len(anchors) != 1:
        raise ValueError("Workspace collapse requires one actual sealed snapshot anchor")
    root = world.store.run_dir(run_id) / "work-snapshot"
    actual = snapshot_manifest(root)
    if any(anchors[0].get(key) != value for key, value in actual.items()):
        raise ValueError("Workspace collapse snapshot differs from its sealed anchor")
    return root, actual, anchors[0]


def _replies(rows):
    for row in rows:
        if row["kind"] == "outcome":
            yield row, row.get("resource") or row.get("reply") or {}
        elif row["kind"] == "event_pushed":
            yield row, (row.get("event") or {}).get("reply") or {}


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _archives(world, run_ids):
    known, refs, authors = {}, {}, {}
    for run_id in run_ids:
        run, rows = _source(world, run_id)
        if run["config"].get("mode", "live") != "live":
            continue
        root = world.store.run_dir(run_id)
        for row, reply in _replies(rows):
            for resource in _objects(reply):
                handle = resource.get("handle")
                if (
                    resource.get("kind", row.get("reply_kind")) != "result"
                    or not isinstance(handle, str)
                    or not handle.startswith("/world/fetched/")
                ):
                    continue
                _read_file(root, handle.lstrip("/"), resource.get("hash"))
                provenance = resource.get("provenance") or {}
                author = provenance.get("author_class") or resource.get("author_class")
                if isinstance(author, str):
                    authors.setdefault(resource["hash"], set()).add(author)
                urls = [
                    value
                    for value in (resource.get("url"), resource.get("final_url"))
                    if isinstance(value, str) and value.startswith(("http://", "https://"))
                ]
                for url in urls:
                    known[urldefrag(url)[0]] = {
                        "run_id": run_id,
                        "record_hash": row["hash"],
                        "body_hash": resource["hash"],
                        "handle": handle,
                    }
                for ref in (
                    row.get("action_id"),
                    row.get("event_id"),
                    row.get("event", {}).get("event_id"),
                    row.get("event", {}).get("action_id"),
                    resource.get("action_id"),
                    resource.get("hash"),
                ):
                    if isinstance(ref, str):
                        refs.setdefault(ref, set()).update(urldefrag(url)[0] for url in urls)
    return known, refs, authors


def _citations(claims, known, refs):
    results = []
    for claim in claims:
        text = claim.get("claim", "")
        urls = {
            urldefrag(url.rstrip(".,;)]}"))[0] for url in re.findall(r"https?://[^\s<>\"'`]+", text)
        }
        if isinstance(claim.get("url"), str):
            urls.add(urldefrag(claim["url"])[0])
        for evidence in claim.get("evidence", []):
            if isinstance(evidence, dict):
                urls.update(refs.get(evidence.get("ref"), set()))
        verified = sorted(urls & known.keys())
        results.append(
            {
                "id": claim.get("id"),
                "verified_live_urls": verified,
                "sources": [known[url] for url in verified],
            }
        )
    return {
        "status": "measured",
        "column": "V",
        "claims": len(claims),
        "with_verified_live_url": sum(bool(row["verified_live_urls"]) for row in results),
        "fraction": sum(bool(row["verified_live_urls"]) for row in results) / len(claims)
        if claims
        else None,
        "items": results,
        "definition": "literal or structured citation to a body hash verified from a successful live-mode source fetch; no current reachability or entailment claim",
    }


def _collapse(world, run, rows):
    root, snapshot, anchor = _snapshot(world, run["run_id"])
    files = {name: entry for name, entry in snapshot["entries"].items() if entry["type"] == "file"}
    owners = {run["run_id"]}
    workspace_retention = unmeasured("No authenticated workspace import receipt")
    imported = [row for row in rows if row["kind"] == "workspace_import"]
    if imported:
        if len(imported) != 1:
            raise ValueError("Workspace import source is ambiguous")
        receipt = imported[0]
        owner = receipt["source_run_id"]
        donor, donor_rows = _source(world, owner)
        _, original, _ = _snapshot(world, owner)
        if (
            run["config"].get("iota", {}).get("workspace") != f"import({owner})"
            or receipt.get("source_record_head") != donor_rows[-1]["hash"]
            or receipt.get("source_snapshot_root_hash") != original["root_hash"]
        ):
            raise ValueError("Workspace import receipt does not bind its actual donor")
        owners.add(donor["run_id"])
        donor_files = {
            name: entry for name, entry in original["entries"].items() if entry["type"] == "file"
        }
        hashes = Counter(entry["sha256"] for entry in files.values())
        original_hashes = Counter(entry["sha256"] for entry in donor_files.values())
        content_retained = sum((hashes & original_hashes).values())
        workspace_retention = {
            "status": "measured",
            "column": "V",
            "source_run_id": owner,
            "source_record_head": donor_rows[-1]["hash"],
            "source_snapshot_root_hash": original["root_hash"],
            "import_receipt_ref": receipt["hash"],
            "imported_files": len(donor_files),
            "same_path_and_bytes_retained": sum(
                files.get(name) == entry for name, entry in donor_files.items()
            ),
            "byte_identical_items_retained_including_renames": content_retained,
            "content_retention_fraction": content_retained / len(donor_files)
            if donor_files
            else None,
        }
    service = world.services.get("beliefs")
    has_store = bool(
        service
        and (
            "belief" in run["config"].get("packs", [])
            or run["config"].get("iota", {}).get("beliefs", "fresh") != "fresh"
        )
    )
    beliefs, retention = [], unmeasured("No actual belief import receipt")
    if has_store:
        if (
            not service.beliefs(run["run_id"]).verify()["valid"]
            or not service.database.verify(run["run_id"])["valid"]
        ):
            raise ValueError("Belief collapse requires verified semantic and kernel chains")
        beliefs = service.fold(run["run_id"])
        imports = [
            row for row in rows if row["kind"] in {"belief_bundle_import", "belief_control_mount"}
        ]
        if imports:
            if len(imports) != 1 or not isinstance(imports[0].get("belief_seq"), int):
                raise ValueError("Belief import has no exact initialization boundary")
            receipt = imports[0]
            owner = receipt["source_run_id"]
            _source(world, owner)
            owners.add(owner)
            initial = service.beliefs(run["run_id"])._fold(
                service.beliefs(run["run_id"])._records()[: receipt["belief_seq"]]
            )
            donor_beliefs = {row["id"]: row for row in service.fold(owner)}
            if any(
                key not in donor_beliefs or donor_beliefs[key]["claim"] != item["claim"]
                for key, item in initial.items()
            ):
                raise ValueError("Belief initialization does not identify actual donor claims")
            final = {row["id"]: row for row in beliefs if row["status"] in ACTIVE}
            retention = {
                "status": "measured",
                "column": "V",
                "source_run_id": owner,
                "import_receipt_ref": receipt["hash"],
                "imported_items": len(initial),
                "active_items_retained": len(initial.keys() & final.keys()),
                "unchanged_claims_retained": sum(
                    final[key]["claim"] == item["claim"]
                    for key, item in initial.items()
                    if key in final
                ),
                "active_retention_fraction": len(initial.keys() & final.keys()) / len(initial)
                if initial
                else None,
            }
    archive_failure = None
    try:
        known, refs, authors = _archives(world, owners)
    except (ValueError, OSError) as error:
        archive_failure = str(error)
        known, refs, authors = {}, {}, {}
    file_authors = {
        name: next(iter(authors[entry["sha256"]]))
        for name, entry in files.items()
        if len(authors.get(entry["sha256"], set())) == 1
        and authors[entry["sha256"]] <= KNOWN_AUTHORS
    }
    third_party = (
        {
            "status": "measured",
            "column": "V",
            "fraction": sum(author in THIRD_PARTY for author in file_authors.values()) / len(files),
            "files": len(files),
            "definition": "byte-identical copied files with explicit source author_class; URL evidence alone establishes no authorship",
        }
        if files and len(file_authors) == len(files)
        else unmeasured(
            "Explicit author_class provenance is absent or conflicting for some workspace bytes",
            known_files=len(file_authors),
            files=len(files),
        )
    )
    workspace_claims = (
        unmeasured("Live source archive verification failed: " + archive_failure)
        if archive_failure
        else unmeasured("No captured out/claims.json; prose claim extraction was not measured")
    )
    if "out/claims.json" in files and not archive_failure:
        claims = json.loads(
            _read_file(root, "out/claims.json", files["out/claims.json"]["sha256"], limit=8_000_000)
        )
        if isinstance(claims, dict):
            claims = claims.get("claims")
        if isinstance(claims, list) and all(
            isinstance(row, dict) and isinstance(row.get("claim"), str) for row in claims
        ):
            workspace_claims = _citations(claims, known, refs)
        else:
            workspace_claims = unmeasured(
                "Captured claims.json does not contain explicit claim records"
            )
    active = [row for row in beliefs if row["status"] in ACTIVE]
    return {
        "workspace": {
            "status": "measured",
            "column": "V",
            "files": len(files),
            "bytes": sum(entry["bytes"] for entry in files.values()),
            "symlinks": sum(entry["type"] == "symlink" for entry in snapshot["entries"].values()),
            "snapshot_root_hash": snapshot["root_hash"],
            "snapshot_anchor": anchor["hash"],
            "retention": workspace_retention,
            "claims": workspace_claims,
            "third_party_authored_fraction": third_party,
        },
        "store": {
            "status": "measured" if has_store else "unmeasured",
            "column": "V",
            "items": len(beliefs) if has_store else None,
            "active_items": len(active) if has_store else None,
            "bytes": len(canonical(beliefs).encode()) if has_store else None,
            "byte_policy": "complete canonical public semantic+kernel projection, including world identifiers",
            "retention": retention,
            "claims": _citations(active, known, refs)
            if not archive_failure
            else unmeasured("Live source archive verification failed: " + archive_failure),
            "third_party_authored_fraction": unmeasured(
                "Belief assertion authors agent/reflector do not establish original authorship of claim content"
            ),
        },
    }


def _t0_profile(world, run, classes):
    life_id = run["config"].get("life_id")
    if not life_id or "lives" not in world.services:
        raise ValueError("No registered life t=0 leisure fingerprint")
    life = world.services["lives"].get(life_id)
    path = world.store.data_dir / "lives" / life_id / "t0.json"
    if path.resolve() != path or not path.is_file():
        raise ValueError("No registered private t=0 artifact")
    baseline = json.loads(path.read_bytes())
    if (
        digest(baseline) != life.get("t0_fingerprint_ref")
        or baseline.get("life_id") != life_id
        or baseline.get("model_snapshot_id") != run["config"].get("model_snapshot_id")
    ):
        raise ValueError("t=0 fingerprint differs from the actual life/model")
    leisure = baseline["leisure"]
    child = leisure["measurement_run_id"]
    spec = JudgeSpec(model=baseline["model_snapshot_id"], family="source")
    _receipt(
        world.store,
        baseline["source_run_id"],
        spec,
        {"measurement": {"measurement_run_id": child, "spent": world.store.spend(child)}},
        lane="probe",
    )
    child_run, rows = _source(world, child)
    if (
        child not in baseline.get("measurement_run_ids", [])
        or child_run["created_at"] >= run["created_at"]
    ):
        raise ValueError("The actual leisure measurement must precede the session")
    calls = [row for row in rows if row["kind"] == "model_call" and row.get("usage")]
    if len(calls) != 1:
        raise ValueError("Leisure profile needs one actual complete tool-less choice call")
    anchors = [
        row
        for row in rows
        if row["kind"] == "model_capture" and row.get("call_id") == calls[0]["call_id"]
    ]
    name = f"model-io/{calls[0]['call_id']}.json"
    if (
        len(anchors) != 1
        or anchors[0].get("capture_ref") != name
        or anchors[0].get("request_hash") != calls[0].get("request_hash")
    ):
        raise ValueError("Leisure capture is not bound to its actual model call")
    capture = json.loads(
        _read_file(world.store.run_dir(child), name, anchors[0]["capture_hash"], limit=8_000_000)
    )
    request, response = capture["request"], json.loads(capture["response"])
    users = [row for row in request.get("messages", []) if row.get("role") == "user"]
    if (
        request.get("model") != spec.model
        or request.get("tools") not in (None, [])
        or request.get("functions")
        or len(users) != 1
    ):
        raise ValueError("Leisure choices were not an actual matching tool-less call")
    data = json.loads(users[0]["content"])
    if (
        data.get("component") != "leisure"
        or not isinstance(data.get("items"), list)
        or not data["items"]
    ):
        raise ValueError("The captured call is not a frozen leisure choice battery")
    if response.get("choices"):
        message = response["choices"][0]["message"]
        if message.get("tool_calls"):
            raise ValueError("Leisure response attempted a tool")
        text = message["content"]
    else:
        if any(row.get("type") == "tool_use" for row in response.get("content", [])):
            raise ValueError("Leisure response attempted a tool")
        text = "".join(
            row.get("text", "") for row in response.get("content", []) if row.get("type") == "text"
        )
    answers = _json_reply({"text": text})
    if answers != leisure.get("answers"):
        raise ValueError("Stored leisure answers differ from the actual captured choices")
    ids, totals = {row["id"] for row in classes}, Counter()
    if set(answers) != {item["item_id"] for item in data["items"]}:
        raise ValueError("Actual leisure answers omit frozen items")
    for item in data["items"]:
        choices, durations = item["choices"], []
        if answers[item["item_id"]] not in choices:
            raise ValueError("Leisure answer is outside the captured choices")
        for choice in choices:
            profile = choice.get("time_allocation", {}) if isinstance(choice, dict) else {}
            seconds = profile.get("seconds", {})
            if (
                profile.get("protocol_id") != LEISURE_POLICY_ID
                or profile.get("classes_hash") != digest(classes)
                or set(seconds) != ids
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    for value in seconds.values()
                )
                or sum(seconds.values()) <= 0
            ):
                raise ValueError("Leisure choices lack the same frozen class-duration protocol")
            durations.append(sum(seconds.values()))
        if len(set(durations)) != 1:
            raise ValueError("Leisure choices must allocate the same total duration per item")
        totals.update(answers[item["item_id"]]["time_allocation"]["seconds"])
    return {
        "seconds": dict(totals),
        "fingerprint_ref": life["t0_fingerprint_ref"],
        "measurement_run_id": child,
        "capture_hash": anchors[0]["capture_hash"],
        "classes_hash": digest(classes),
        "protocol_id": LEISURE_POLICY_ID,
        "definition": "actual pre-life choices over frozen equal-duration class allocations; a revealed choice profile, not earlier elapsed attention",
    }


def _topics(world, run, rows, measurements):
    payload = measurements.get("allocation") if isinstance(measurements, dict) else None
    if not isinstance(payload, dict):
        return unmeasured(
            "No actual captured allocation labels; entity mentions alone are not topic time tags"
        )
    try:
        from .classifier_evidence import verify_saved_allocation
        from .instruments import load_artifact

        verified = verify_saved_allocation(world, run["run_id"], payload)
        instrument = json.loads(
            load_artifact(world.store, run["run_id"], verified["instrument_ref"])[0]
        )
        classes = instrument["classes"]
        readings = {
            row["action_ref"]: row
            for segment in verified["segments"]
            for row in segment["readings"]
        }
        actions = [row for row in rows if row["kind"] == "action" and row.get("world_action", True)]
        durations, unknown, invalid = Counter(), 0.0, False
        for index, action in enumerate(actions):
            stop = (
                actions[index + 1]["ts"]
                if index + 1 < len(actions)
                else next((row["ts"] for row in rows if row["kind"] == "beam_up"), rows[-1]["ts"])
            )
            seconds = _seconds(action["ts"], stop)
            if seconds is None:
                invalid = True
                continue
            label = readings.get(action["action_id"], {}).get("content_class")
            if label is None:
                unknown += seconds
            else:
                durations[label] += seconds
        total = sum(durations.values()) + unknown
        agreement = verified["calibration"]["content_class"]
        result = {
            "status": "measured",
            "column": agreement["column"],
            "classes": classes,
            "seconds": {row["id"]: durations[row["id"]] for row in classes},
            "unclassified_seconds": unknown,
            "coverage": sum(durations.values()) / total if total else None,
            "calibration": agreement,
            "allocation_protocol_hash": verified["protocol_hash"],
            "allocation_instrument_ref": verified["instrument_ref"],
            "time_policy": "elapsed intervals from each ordinary world action to the next or beam_up; temporal allocation, not a causal attention measure",
            "leisure_js_divergence": unmeasured(
                "Complete calibrated topic time and an actual matched t=0 profile are required"
            ),
        }
        if agreement["column"] != "V" or invalid or unknown or total <= 0:
            return result
        try:
            baseline = _t0_profile(world, run, classes)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            result["leisure_js_divergence"] = unmeasured(
                "No verified matching t=0 leisure profile: " + str(error)
            )
            return result
        names = [row["id"] for row in classes]
        result["leisure_js_divergence"] = {
            "status": "measured",
            "column": "V",
            "value": jensen_shannon(
                [durations[name] for name in names], [baseline["seconds"][name] for name in names]
            ),
            "log_base": 2,
            "classes": names,
            "source_seconds": {name: durations[name] for name in names},
            "t0": baseline,
            "causal_interpretation": False,
        }
        return result
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        return unmeasured("Topic/allocation or t=0 provenance is unavailable: " + str(error))


def signature_details(world, run_id, measurements=None):
    """Assemble actual passive details without changing the fixed F7 ICC projection."""
    run, rows = _source(world, run_id)
    if measurements is None:
        from .publication import load_measurements

        measurements = load_measurements(world.store, run_id)
    actions = [row for row in rows if row["kind"] == "action" and row.get("world_action", True)]
    models = [
        row
        for row in rows
        if row["kind"] == "model_call"
        and not {"model.reflect", "model.reflector", "model.perception"}.intersection(
            row.get("tags", [])
        )
    ]
    first = actions[0] if actions else None
    latency = (
        {
            "status": "measured",
            "column": "V",
            "seconds": _seconds(run["created_at"], first["ts"]),
            "usd": sum(
                row.get("amount_nano", 0)
                for row in rows
                if row["seq"] < first["seq"] and row["kind"] == "ledger_debit"
            )
            / 1e9,
            "ordinary_model_calls": sum(row["seq"] < first["seq"] for row in models),
            "action_ref": first["action_id"],
            "record_hash": first["hash"],
        }
        if first
        else unmeasured("No ordinary world action was submitted")
    )
    reads = [row for row in rows if row["kind"] == "inbox_read"]
    events = [row["event"] for row in rows if row["kind"] == "event_pushed"]
    read_events = {
        event["event_id"]
        for event in events
        if any(read.get("since", 0) < event["cursor"] <= read["cursor"] for read in reads)
    }
    tickets = [row for row in rows if row["kind"] == "action_ticket"]
    async_outcomes = [
        row for row in rows if row["kind"] == "outcome" and row.get("delivered") == "async"
    ]
    concurrency = [
        row["in_flight_before"] for row in actions if isinstance(row.get("in_flight_before"), int)
    ]
    trajectory = trajectory_metrics(run, rows)
    return {
        "status": "measured",
        "policy_id": POLICY_ID,
        "source_run_id": run_id,
        "source_record_head": rows[-1]["hash"],
        "ordinary_model_calls": {
            "status": "measured",
            "column": "V",
            "count": len(models),
            "definition": "ordinary model-call records, excluding internal reflection/perception; harness-level logical turns are not inferred",
        },
        "world_actions": {"status": "measured", "column": "V", "count": len(actions)},
        "mean_in_flight": {
            "status": "measured",
            "column": "V",
            "value": sum(concurrency) / len(concurrency),
        }
        if concurrency and len(concurrency) == len(actions)
        else unmeasured("Complete recorded action concurrency is unavailable"),
        "time_to_first_world_action": latency,
        "ticket_use": {
            "status": "measured" if tickets else "unmeasured",
            "column": "V",
            "exact_receipts": len(tickets) if tickets else None,
            "known_async_outcomes": len(async_outcomes),
            "receipt_refs": [row["hash"] for row in tickets],
            "reason": None
            if tickets
            else "No exact ticket-return receipts were recorded; asynchronous outcomes are reported separately",
        },
        "inbox_use": {
            "status": "measured",
            "column": "V",
            "reads": len(reads),
            "events_delivered": len(events),
            "unique_events_read": len(read_events),
            "event_types_read": dict(
                Counter(
                    event.get("type", "unknown")
                    for event in events
                    if event["event_id"] in read_events
                )
            ),
            "cursor_receipt_refs": [row["hash"] for row in reads],
        },
        "voluntary_stop": {
            "status": "measured",
            "column": "V",
            "value": (run.get("end") or {}).get("reason") == "agent_finished",
        },
        "unique_domains": {
            "status": "measured",
            "column": "V",
            "count": len(trajectory["discovery"]["domains"]),
            "domains": trajectory["discovery"]["domains"],
        },
        "revisit_ratio": {
            "status": "measured"
            if trajectory["behavior"]["revisit_ratio"] is not None
            else "unmeasured",
            "column": "V",
            "value": trajectory["behavior"]["revisit_ratio"],
        },
        "ask_stream": {
            "status": "measured",
            "column": "V",
            "items": [
                {
                    "ticket_id": row.get("ticket_id"),
                    "kind": row.get("kind_requested"),
                    "record_hash": row["hash"],
                    "seq": row["seq"],
                }
                for row in rows
                if row["kind"] == "ask_opened"
            ],
        },
        "spend_decomposition": {
            "status": "measured",
            "column": "V",
            "by_category": world.store.spend(run_id),
            "phases": trajectory["spend_phases"],
            "token_classes": trajectory["token_classes"],
            "tokens_per_world_action": trajectory["tokens_per_world_action"],
        },
        "topics": _topics(world, run, rows, measurements),
        "collapse": _collapse(world, run, rows),
        "F7_ICC_projection_unchanged": True,
        "scientific_gate_evidence": False,
    }
