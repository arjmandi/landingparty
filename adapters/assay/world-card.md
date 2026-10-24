# Landing Party world card

| Field | Value |
|---|---|
| World | `landing-party` |
| Archetype | service |
| Backend | the world's agent HTTP interface, through `adapter.py:factory` |
| Actions | each world tool from the run's manifest; outward tools are destructive |
| Observations | results, typed blocks, tickets, silence, inbox events, and state |
| Reward | `None` in live mode |
| Replay | none: one daemon per world session (`replayable=False`) |
| Budget | USD and wall seconds; ASSAY cap above world cap |
| Isolation | cooperative by default; fenced enforcement is a separate deployment requirement |
| Agent predictions | ASSAY policy; never world infrastructure |

See `00-landing-party.md`, appendix A, for the design's full ASSAY card and the
README for which deployment capabilities have actually been validated.
