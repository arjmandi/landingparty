# ASSAY as a native harness

[ASSAY](https://github.com/arjmandi/assay) is a prediction-gated harness: every
paid action carries the player's prediction, which ASSAY grades against the
world's reply and keeps in a hash-chained journal. ASSAY never calls a model. A
**player**, another native client, acts on the world only through ASSAY's
command line. In Landing Party, ASSAY 1.5.1 is a native harness that plays
through **Claude Code**, on Claude Code's connection (its API key or its
subscription account), model and effort.

## The world is the operator

ASSAY's `/assay:attach` skill and its session runner do the operator's work for a
person. Here the world does it, deterministically, for each session (one ASSAY
run per world session; live world actions cannot be replayed):

1. **Registry.** The world writes ASSAY's registry from the run's own tool
   manifest (`worldd/assay.py`). Each world tool is one action. An action takes
   its parameters flat when all are required scalars, as one `arguments` object
   otherwise, and none when the tool takes none; a free-form object travels as
   JSON text the adapter parses back. Read tools and the world's own records
   (beliefs, sleep, wait) are ordinary actions; outward actions (deliver, ask,
   trades, claims) are marked destructive, so ASSAY requires a declared worst
   case and recovery. The prediction gate stays at its required default. The
   dollar cap is the session's cap plus 5%, so the world's binds first. The
   paid-action cap is the run's `harness_options.actions` (1,000 unless set).
2. **Daemon.** `assay start landing-party --adapter adapters/assay/adapter.py:factory`
   runs with the run's capability in the daemon's environment only. The adapter
   (standard library only, inside ASSAY's interpreter) sends each action to the
   world's agent endpoint as `{run_id, tool, params}`; predictions never reach
   the world.
3. **Hooks.** `assay hooks install` writes ASSAY's per-run hooks, refusing the
   player any `spend` report and any call to the world's own address; the world
   adds ASSAY's Stop gate. Because the player runs with `--setting-sources ""`,
   the world copies the hooks into the player's settings file.
4. **Player.** Claude Code starts in the ASSAY run directory with no world tool
   server and **without the run's capability**: on an API key its model calls
   carry a lane-only capability the agent boundary refuses. Its briefing is the
   world's own, followed by how to act through ASSAY. Its notes from the life's
   previous session are carried into `.assay/NOTES.md`.
5. **Close.** The world posts its running spend to ASSAY every minute, then runs
   `assay audit --json` and `assay stop`, and records `assay_journal` (journal
   hash, events, predictions and how many were right, the audit, and the notes)
   in the run's record.

Metering is the host's: the lane for an API key, the transcript estimate for a
Claude subscription. A Studio life on ASSAY holds Claude Code's account, so
Claude Code cannot run beside it.

## Install

The world image clones ASSAY's `v1.5.1` tag, checks it against commit
`44d8f08`, and installs it into its own Python 3.12 environment
(`WORLD_ASSAY_CLI`, `ASSAY_PYTHON`); `procps` is included for ASSAY's daemon
control. Running `worldd` directly, point those two variables at an ASSAY
checkout and its interpreter.

## Limits

- Isolation is cooperative: the player runs as the same OS user as the daemon,
  so the hooks and missing capability guide it rather than confine it.
- ASSAY on Codex waits for Codex's subscription metering, and Codex has no
  PreToolUse or Stop hooks.
- The registry's hash is recorded with each session (`assay_registry_hash`)
  rather than in the version tuple, because the registry is written at launch.
