"""Validate scientific life controls against actual inducing conditions."""

from .storage import canonical_json, content_hash

_IGNORED = {
    "seed",
    "seed_scope",
    "life_id",
    "identity_id",
    "series_id",
    "continuity_source_run_id",
    "preamble_hash",
    "preflight_id",
    "t0_fingerprint_ref",
}


def effective_life_config(world, config):
    """Apply the same immutable life conditions used when a session really opens."""
    if not config.get("life_id"):
        return config
    life = world.services["lives"].get(config["life_id"])
    result = dict(config)
    result.update(
        {
            key: life[key]
            for key in (
                "stage",
                "research_mode",
                "continuity",
                "wake_policy",
                "reply_policy",
                "driver",
            )
        }
    )
    for key in ("binding_id", "identity_id"):
        if life[key] is not None:
            result[key] = life[key]
    return result


def _conditions(world, cell):
    config = effective_life_config(world, cell["config"])
    life = world.services["lives"].get(config["life_id"])
    protocol = world.services["lives"].protocol_identity(life["life_id"])
    conditions = {key: value for key, value in config.items() if key not in _IGNORED}
    conditions["iota"] = {
        key: "import" if isinstance(value, str) and value.startswith("import(") else value
        for key, value in conditions.get("iota", {}).items()
    }
    for key in ("budget_nano", "wall_life_days", "afterlife_days", "income_rail"):
        conditions["life." + key] = protocol[key]
    conditions["driver"] = protocol["driver"]
    return conditions


def validate_life_design(world, cells, prereg):
    selected = [cell for cell in cells if cell["config"].get("life_id")]
    if not selected or not any(cell["config"]["research_mode"] == "research" for cell in selected):
        return None
    if len(selected) != len(cells) or any(
        cell["config"]["research_mode"] != "research" for cell in selected
    ):
        raise ValueError("A scientific life study cannot mix episode or development cells")
    specification = prereg.get("life_design")
    if not isinstance(specification, dict) or set(specification) - {
        "centre_cell",
        "global_control_cell",
        "factor_fields",
    }:
        raise ValueError("Research life studies require an explicit frozen life_design")
    by_id = {cell["cell_id"]: cell for cell in selected}
    groups = {}
    life_ids = []
    for cell in selected:
        if len(cell["seeds"]) != 1:
            raise ValueError("Independent scientific seeds require distinct actual lives")
        life_ids.append(cell["config"]["life_id"])
        groups.setdefault(cell.get("condition_id", cell["cell_id"]), []).append(cell)
    if len(set(life_ids)) != len(life_ids):
        raise ValueError("Scientific cells cannot reuse one actual life as independent replicates")
    if any(len({cell["seeds"][0] for cell in group}) != len(group) for group in groups.values()):
        raise ValueError("A scientific condition may have one actual life per seed")
    centre_id, control_id = (
        specification.get("centre_cell"),
        specification.get("global_control_cell"),
    )
    centre_id = by_id[centre_id].get("condition_id", centre_id) if centre_id in by_id else centre_id
    control_id = (
        by_id[control_id].get("condition_id", control_id) if control_id in by_id else control_id
    )
    if centre_id not in groups or control_id not in groups:
        raise ValueError("The actual centre and global-control cells must be present")
    factors = specification.get("factor_fields")
    if (
        not isinstance(factors, list)
        or len(factors) != len(set(factors))
        or any(not isinstance(value, str) for value in factors)
    ):
        raise ValueError("Register the explicit varied factor fields")
    all_conditions = {name: _conditions(world, cell) for name, cell in by_id.items()}
    conditions = {}
    for name, group in groups.items():
        examples = [all_conditions[cell["cell_id"]] for cell in group]
        if any(canonical_json(example) != canonical_json(examples[0]) for example in examples[1:]):
            raise ValueError("Replicates must retain their actual named inducing conditions")
        conditions[name] = examples[0]
    centre, control = conditions[centre_id], conditions[control_id]
    if (
        control["driver"] is not None
        or control.get("goal") is not None
        and control.get("goal") != ""
        or control["vault_mode"] != "identity_only"
        or control["reply_policy"] != "silent"
        or control.get("operator_messages_allowed")
    ):
        raise ValueError(
            "The actual global control requires no driver or goal, identity_only vault, and silence"
        )
    for name, condition in conditions.items():
        if condition["driver"] is not None and condition["vault_mode"] != "identity_only":
            raise ValueError("Driver and vault treatments must never be crossed")
        if set(condition) != set(centre):
            raise ValueError("Scientific cells must measure the same inducing fields")
    r5 = str(prereg.get("experiment_id", "")).upper() == "R5"
    if r5 and set(factors) != {"reply_policy", "vault_mode"}:
        raise ValueError("R5 permits only the registered reply_policy by vault_mode factorial")
    for name, condition in conditions.items():
        if name == control_id and not r5:
            continue
        differences = {
            key for key in centre if canonical_json(centre[key]) != canonical_json(condition[key])
        }
        if not differences <= set(factors) or (not r5 and len(differences) > 1):
            raise ValueError(
                "Scientific life cells vary one registered factor around the actual centre"
            )
        for cell in groups[name]:
            declared = cell.get("varied_factors", [])
            if not isinstance(declared, list) or set(declared) != differences:
                raise ValueError(
                    "Cell varied_factors must match the actual inducing-condition differences"
                )
    if r5:
        expected = {
            (reply, vault)
            for reply in ("silent", "blockers_only", "blockers_plus_checkins")
            for vault in ("identity_only", "catalogue", "on_ask")
        }
        actual = [(item["reply_policy"], item["vault_mode"]) for item in conditions.values()]
        if (
            len(actual) != 9
            or set(actual) != expected
            or any(item["driver"] for item in conditions.values())
        ):
            raise ValueError(
                "R5 must retain its complete driver-free three by three reply/vault schedule"
            )
    minimum = (
        20
        if centre.get("goal") is None or centre.get("goal") == ""
        else 10
        if prereg.get("headline") is True
        else 5
    )
    declared_seeds = prereg.get("seeds")
    required = max(
        minimum,
        len(declared_seeds)
        if isinstance(declared_seeds, list)
        else prereg.get("seed_count", minimum),
    )
    seed_prerequisites = []
    centre_seeds = {cell["seeds"][0] for cell in groups[centre_id]}
    for name, group in groups.items():
        seeds = {cell["seeds"][0] for cell in group}
        if (
            len(seeds) < required
            or seeds != centre_seeds
            or isinstance(declared_seeds, list)
            and not set(declared_seeds) <= seeds
        ):
            seed_prerequisites.append(
                {
                    "cell": name,
                    "requirement": "independent_life_seeds",
                    "detail": f"Register at least {required} distinct actual life/seed pairs per condition with matched seed sets",
                }
            )
    return {
        "specification": specification,
        "condition_hashes": {
            name: content_hash(condition) for name, condition in all_conditions.items()
        },
        "scientific_gate_passed": False,
        "prerequisites": seed_prerequisites,
    }
