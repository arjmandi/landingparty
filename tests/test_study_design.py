"""Scientific plan structure is checked before any agent run can start."""

import copy

import pytest

from worldd.app import create_app


@pytest.fixture
async def design_world(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    world = app.state.world
    # These tests isolate plan validation; no gate is registered or run executed.
    monkeypatch.setattr(world.services["registry"], "require_stage", lambda *args, **kwargs: {})
    yield world
    await world.aclose()


def specification():
    return {
        "experiment_id": "L2",
        "primary_metric": {"name": "experience", "threshold": 1},
        "seed_count": 5,
        "seed_count_rationale": "Engineering plan fixture",
        "stopping_rule": "Every started life retained",
        "likely_null": "No difference",
        "exploratory_metrics": [],
        "life_design": {
            "centre_cell": "centre",
            "global_control_cell": "global",
            "factor_fields": ["rubric_disclosed"],
        },
    }


def cell(world, name, *, reply="silent", driver=None, **config):
    life = world.services["lives"].create(
        {
            "stage": 2,
            "research_mode": "research",
            "reply_policy": reply,
            "budget": {"usd_life": 20, "wall_life_days": 14},
            "driver": driver,
        }
    )
    return {
        "cell_id": name,
        "baseline": "B1",
        "seeds": [0],
        "model": "fixture-model",
        "config": {"life_id": life["life_id"], "packs": [], **config},
        "varied_factors": [] if name in {"centre", "global"} else ["rubric_disclosed"],
    }


async def test_research_life_plan_needs_actual_global_control_and_one_factor(design_world):
    world = design_world
    cells = [
        cell(world, "centre"),
        cell(world, "global"),
        cell(world, "named", rubric_disclosed="named"),
    ]
    plan = world.services["studies"].plan({"prereg": specification(), "cells": cells})
    assert plan["life_design"]["condition_hashes"]
    assert world.store.list_runs() == []
    invalid = copy.deepcopy(cells)
    invalid[1]["config"]["goal"] = "A planted rule pretending to be control"
    with pytest.raises(ValueError, match="global control"):
        world.services["studies"].plan({"prereg": specification(), "cells": invalid})
    invalid = copy.deepcopy(cells)
    invalid[2]["config"]["budget_visible"] = False
    with pytest.raises(ValueError, match="one registered factor"):
        world.services["studies"].plan({"prereg": specification(), "cells": invalid})
    with pytest.raises(ValueError, match="centre and global-control"):
        world.services["studies"].plan({"prereg": specification(), "cells": cells[:1]})
    assert world.store.list_runs() == []


async def test_control_role_cannot_override_actual_life_reply_or_driver(design_world):
    world = design_world
    cells = [cell(world, "centre"), cell(world, "global", reply="blockers_only")]
    cells[1]["config"]["reply_policy"] = "silent"
    with pytest.raises(ValueError, match="global control"):
        world.services["studies"].plan({"prereg": specification(), "cells": cells})
    assert world.store.list_runs() == []


async def test_independent_replicates_use_distinct_lives_and_stable_actual_condition(design_world):
    world = design_world
    cells = []
    for seed in (0, 1):
        for name in ("centre", "global"):
            value = cell(world, name)
            value.update(cell_id=f"{name}.{seed}", condition_id=name, seeds=[seed])
            cells.append(value)
    plan = world.services["studies"].plan({"prereg": specification(), "cells": cells})
    assert len(plan["life_design"]["condition_hashes"]) == 4
    assert any(item["requirement"] == "independent_life_seeds" for item in plan["prerequisites"])
    with pytest.raises(ValueError, match="independent_life_seeds"):
        await world.services["studies"].execute(plan["study_id"], execute=True, budget_usd=1000)
    invalid = copy.deepcopy(cells)
    invalid[0]["seeds"] = [0, 1]
    with pytest.raises(ValueError, match="distinct actual lives"):
        world.services["studies"].plan({"prereg": specification(), "cells": invalid})
    invalid = copy.deepcopy(cells)
    invalid[2]["config"]["life_id"] = invalid[0]["config"]["life_id"]
    with pytest.raises(ValueError, match="reuse one actual life"):
        world.services["studies"].plan({"prereg": specification(), "cells": invalid})
    assert world.store.list_runs() == []


async def test_r5_preserves_exact_three_by_three_and_never_driver_vault(design_world):
    world = design_world
    cells = []
    for reply in ("silent", "blockers_only", "blockers_plus_checkins"):
        for vault in ("identity_only", "catalogue", "on_ask"):
            name = "global" if (reply, vault) == ("silent", "identity_only") else f"{reply}.{vault}"
            if (reply, vault) == ("blockers_only", "identity_only"):
                name = "centre"
            value = cell(world, name, reply=reply, vault_mode=vault)
            value["varied_factors"] = (["reply_policy"] if reply != "blockers_only" else []) + (
                ["vault_mode"] if vault != "identity_only" else []
            )
            cells.append(value)
    prereg = specification()
    prereg.update(experiment_id="R5")
    prereg["life_design"]["factor_fields"] = ["reply_policy", "vault_mode"]
    plan = world.services["studies"].plan({"prereg": prereg, "cells": cells})
    assert plan["life_design"] and world.store.list_runs() == []
    with pytest.raises(ValueError, match="complete driver-free"):
        world.services["studies"].plan({"prereg": prereg, "cells": cells[:-1]})
    driver = {
        "metric": "usd_balance",
        "reading_at_start": 10,
        "direction": "down",
        "floor": 0,
        "floor_event": "bankruptcy",
        "rescue_policy": "none",
    }
    invalid = [cell(world, "centre", driver=driver, vault_mode="catalogue"), cell(world, "global")]
    with pytest.raises(ValueError, match="never be crossed"):
        world.services["studies"].plan({"prereg": specification(), "cells": invalid})
    assert world.store.list_runs() == []
