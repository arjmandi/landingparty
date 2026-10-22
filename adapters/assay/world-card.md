# Landing Party world card

| Field | Value |
|---|---|
| World | `landing-party` |
| Archetype | service |
| Backend | MCP (`world-mcp`) or the same core HTTP interface |
| Actions | typed world tools from `/v1/manifest`; no writes or accounts at stage 0 |
| Observations | results, typed blocks, tickets, silence, inbox events, and state |
| Reward | `None` in live mode |
| Replay | recorded archive only; replay misses remain explicit observations |
| Budget | USD and wall seconds; ASSAY cap above world cap |
| Isolation | cooperative by default; fenced enforcement is a separate deployment requirement |
| Agent predictions | ASSAY policy; never world infrastructure |

See `00-landing-party.md`, appendix A, for the design's full ASSAY card and the
README for which deployment capabilities have actually been validated.
