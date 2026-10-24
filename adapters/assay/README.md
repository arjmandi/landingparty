# ASSAY adapter

`adapter.py` is the world adapter ASSAY's daemon imports: `factory(root, config)`
returns a session whose `step(action, data, reasoning)` sends one world tool to
the agent endpoint (`POST /v1/act {run_id, tool, params}`) with the run's
capability, and whose `observation` carries the world's reply (a long reply is
kept whole in `world-results/`). It uses the standard library only, because it
runs in ASSAY's interpreter. Predictions and reasoning stay in ASSAY's journal;
the world never receives them. Search and fetch are sent with `wait=true`, so the
act carries its result. `assay_budget()` puts ASSAY's dollar cap above the
world's by a positive margin.

The daemon's environment names the world (`WORLD_BASE_URL`, `WORLD_RUN_ID`,
`WORLD_RUN_TOKEN`) and the action map (`WORLD_ASSAY_TOOLS`) the world writes with
the registry. The player never holds the run's capability.

The world operates the run itself; see [ASSAY as a native harness](../../docs/assay.md).
