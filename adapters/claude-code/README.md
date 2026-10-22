# Claude Code cooperative binding

Generate a binding for an already-created run:

```bash
python adapters/claude-code/configure.py --run-id r_EXAMPLE --output data/binding-r_EXAMPLE
```

Use the actual run identifier and save the `preamble` from `lp energize` verbatim
as `data/briefing.txt`. Put the goal, if any, in a separate file. The launcher
passes both as literal arguments; it does not interpret shell syntax in them:

```bash
python adapters/claude-code/run.py data/binding-r_EXAMPLE \
  --briefing data/briefing.txt --goal goals/14-audit/goal.md \
  --transcript data/claude-transcript.jsonl
lp beam-up r_EXAMPLE
```

The binding disables provider-side `WebSearch` and `WebFetch`, registers only
the world's MCP server, and points Anthropic traffic at the metered lane. Set
`WORLD_RUN_TOKEN` to the capability returned when the run was created. The
launcher injects the run headers and uses that capability to authenticate to
the local lane; the lane strips it before using a configured world-held
provider key. Binding files never contain the capability. The launcher removes
researcher control tokens and world-held provider keys from the child process.

`--gateway-url` and `--ca-path` must be supplied together and only when a real
gateway and CA are running. Omitting them leaves shell traffic unaudited; this
binding is **cooperative**, not a fenced profile or a completeness guarantee.
The launcher does not start/kill a run: its caller owns the run lifecycle.

No continuation hook is installed. Claude Code does not expose a sampling
seed; the configured seed is a label. Integration tests cover generation and
argument handling. A native Claude Code 2.1.308 fixture pilot also exercised
the generated launcher, two concurrent MCP fetches, streaming model replies,
capability stripping, exact fixture usage metering, and a valid sealed record.
It used injected website/provider transports and made no paid calls.

Run this optional software check explicitly with an installed native CLI:

```bash
.venv/bin/python tests/native_claude_smoke.py --claude /path/to/claude
```

The pilot uses a fresh HOME, sanitized process environment, disabled telemetry,
and a denying fallback proxy. It is a cooperative software check, not a claim
of enforced network isolation. Paid live invocation and the twelve-run
scientific shakedown still require provider access and registered execution.
