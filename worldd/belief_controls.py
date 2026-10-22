"""Explicit experimental initial conditions over an authenticated donor bundle.

The donor signature authenticates the original import. The recipient's separate
initialization receipt describes the randomized control; it never represents a
changed rank as a new donor assertion or as a mechanical kernel observation.
"""

import copy
import random
from collections import defaultdict, deque

from .util import canonical, digest, identifier

POLICY_ID = "transfer-shuffled-ranks-links-bytes@2"


def _graph_projection(records):
    """Rebuild graph-only feedback from authenticated direct observations.

    Direct counterevidence remains attached to its original claim. Its propagated
    from/hop paths must follow the mounted graph rather than disclose donor edges.
    No missing external node or observation is invented to complete that graph.
    """
    state = {record["id"]: record for record in records}
    roots, attachments = defaultdict(set), defaultdict(set)
    for record in records:
        kernel = record.get("kernel", {})
        kernel["support_lost"] = []
        if "flags" in kernel:
            kernel["flags"] = [flag for flag in kernel["flags"] if flag != "support_lost"]
        for evidence in kernel.get("evidence", []):
            attachments[evidence["ref"]].add(record["id"])
        for classification in kernel.get("classifications", []):
            roots[classification["observation_ref"]].add(record["id"])
    for ref, sources in sorted(roots.items()):
        queue, visited = deque((source, 0) for source in sorted(sources)), set(sources)
        affected = set(attachments[ref]) | sources
        while queue:
            source, depth = queue.popleft()
            if depth >= 3:
                continue
            children = sorted(
                belief_id
                for belief_id, record in state.items()
                if record["status"] not in {"retracted", "superseded"}
                and {"to": source, "relation": "derived_from"} in record.get("links", [])
            )
            for child in children:
                if child in visited or (child not in affected and len(affected) >= 50):
                    continue
                visited.add(child)
                affected.add(child)
                kernel = state[child]["kernel"]
                kernel["support_lost"].append(
                    {"observation_ref": ref, "from": source, "hop": depth + 1}
                )
                if "flags" in kernel and "support_lost" not in kernel["flags"]:
                    kernel["flags"].append("support_lost")
                queue.append((child, depth + 1))


def prepare_control(bundle, seed):
    original = copy.deepcopy(bundle["projection"])
    original_bytes = len(canonical(original).encode())
    selected = None
    for attempt in range(256):
        rng = random.Random(f"{POLICY_ID}/{seed}/{attempt}")
        ranks = [row["rank"] for row in original]
        links = [row.get("links", []) for row in original]
        rng.shuffle(ranks)
        rng.shuffle(links)
        shuffled = copy.deepcopy(original)
        for row, rank, edges in zip(shuffled, ranks, links, strict=True):
            direct_contradictions = max(
                0,
                row.get("n_contra", 0)
                - sum(edge["relation"] == "contradicts" for edge in row.get("links", [])),
            )
            row["rank"], row["links"] = rank, edges
            row["n_contra"] = direct_contradictions + sum(
                edge["relation"] == "contradicts" for edge in edges
            )
        _graph_projection(shuffled)
        if shuffled != original and len(canonical(shuffled).encode()) == original_bytes:
            selected = shuffled
            break
    if selected is None:
        raise ValueError(
            "A shuffled control requires a donor with distinct ranks or link assignments"
        )
    return {
        "projection": selected,
        "receipt": {
            "policy_id": POLICY_ID,
            "source_bundle_hash": digest(bundle),
            "source_run_id": bundle["manifest"]["source_run_id"],
            "source_projection_hash": digest(original),
            "projection_hash": digest(selected),
            "seed": seed,
            "matched_utf8_bytes": original_bytes,
            "token_matching": "unmeasured; transfer control preserves encoded bytes",
            "world_experimental_initialization": True,
            "agent_endorsed": False,
            "graph_projection": "direct counterevidence preserved; depth3/n50 propagation recomputed within mounted nodes only",
        },
    }


def apply_control(service, run_id, bundle, control):
    """Initialize only the control projection; retain donor proof privately."""
    if control["receipt"]["source_bundle_hash"] != digest(bundle):
        raise ValueError("Shuffled initialization differs from its authenticated donor")
    service.validate_bundle(bundle)
    beliefs = service.beliefs(run_id)
    if beliefs.fold():
        raise ValueError("A shuffled initialization requires a fresh recipient store")
    directory = service.store.run_dir(run_id) / "belief-controls"
    directory.mkdir(mode=0o700)
    with (directory / "authenticated-donor.json").open("x", encoding="utf-8") as output:
        output.write(canonical(bundle))
    (directory / "authenticated-donor.json").chmod(0o600)
    with service.database.lock:
        for ref, metadata in bundle.get("evidence_index", {}).items():
            service.database.db.execute(
                "INSERT INTO belief_imports VALUES(?,?,?)",
                (
                    run_id,
                    ref,
                    canonical(
                        {
                            **metadata,
                            "run_id": run_id,
                            "imported_from": bundle["manifest"]["source_run_id"],
                        }
                    ),
                ),
            )
        for skill in bundle.get("skills", []):
            service.database.db.execute(
                "INSERT INTO belief_skills VALUES(?,?,?,?)",
                (run_id, skill["name"], skill["version_id"], canonical(skill)),
            )
    for projected in control["projection"]:

        def operation(events, state, stamp, projection=projected):
            result = copy.deepcopy(projection)
            result["imported_kernel"] = result.pop("kernel", {})
            result.update(
                op="assert",
                recorded_at=stamp,
                version_id=identifier("bv"),
                initial_condition=control["receipt"],
                flags=[],
            )
            result.pop("supersedes_version", None)
            return result

        beliefs._transact(operation)
    service.database.audit(
        run_id,
        "belief_control_mount",
        **control["receipt"],
        count=len(control["projection"]),
        belief_seq=len(beliefs._records()),
    )
    service.render(run_id)
