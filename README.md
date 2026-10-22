# Landing Party

A recorded, budgeted world for agents on the live internet, implemented from
[the compiled design](00-landing-party.md). Services cover all four stages,
with research entry gates locked until actual study evidence is registered.
Software tests do not satisfy the design's empirical definition of done.

## Develop and run

Use Python 3.12+ and `uv` from this checkout:

```bash
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --frozen --all-extras
uv run --no-sync pytest -q
uv run --no-sync worldd --data-dir data
```

The full extra installs Playwright, PDF parsing and additional extractors.
Browser operation also needs a sandbox-capable Chromium and `certutil`; see
[deployment](deploy/README.md). `WORLD_CHROMIUM_EXECUTABLE` selects a trusted
system Chromium. The browser does not disable its sandbox or ignore TLS errors.

The process binds loopback by default:

| Port | Interface | Authority |
| --- | --- | --- |
| 7777 | Researcher control, bridge and reports | `WORLD_CONTROL_TOKEN` when configured; mandatory for research |
| 7778 | Agent tools, inbox, manifest and status | Run-scoped bearer token returned on creation/activation |
| 8081 | OpenAI/Anthropic model lane | Run-scoped token; provider keys remain world-held |
| 8080 | Recorded HTTP/HTTPS proxy | Per-run credentials and the world CA |

Keep control outside the agent's namespace. The [fenced deployment](deploy/README.md)
provides default-drop egress and separate users. Cooperative local development
does not isolate shell traffic. The trusted `/bridge` console provides ticket
decisions, active human-time accounting, credential deposit and browser takeover.

In a second terminal, this workflow needs no credentials or paid calls:

```bash
source .venv/bin/activate
lp health
lp energize examples/local-run.json
lp act RUN_ID status
lp act RUN_ID deliver --params '{"kind":"note","body":"First artifact"}'
lp beam-up RUN_ID
lp report RUN_ID
lp export RUN_ID --output /tmp/episode.tar.gz
python scripts/smoke.py
```

Replace `RUN_ID` with the creation result. Keep its token in the agent's scoped
launch environment. Do not give provider/control credentials to the agent. Mount
the returned workspace at `/work` and world directory read-only at `/world`;
the local launcher itself does not create these mounts.

## Implemented surfaces

- Core tools, asynchronous jobs/tickets/inbox, concurrency, deadlines, breakers,
  freeze/resume, emergency stop, restart recovery, sealing and export.
- Atomic nanodollar accounting, conservative reservations, JSON/SSE model usage,
  cache pricing, hosted-web refusal and redacted model capture.
- Ordered gateway policy, destination pinning, robots/rates, CA/HTTPS proxy,
  signatures, WARC/HAR, explicit extraction, indexed replay and frozen corpora.
- All declared packs: forecast, watch, deliver, belief, schedule, reflect,
  vault, browse, mail, social, footprint, perception and market.
- Append-only beliefs, late-evidence kernel, expectations, continuity/transfer,
  immutable workspace-note citations, proposed triple slots, isolated skill
  testing/promotion, metered reflection, life budgets, scarcity and sleep.
- Encrypted world-held credentials, scoped leases, exact approvals, signed
  capability grants with rehearsal, terms/disclosure, mandates and incidents.
- Identity/mail/social adapters, paper markets/bounties, perception quotes,
  footprint/afterlife readings, consented arenas and opt-in surveys.
- Post-seal audits, metered forked probes/t=0 batteries, interviews, calibrated
  cross-family judging, controlled value/taste/formation/divergence runners,
  prediction-loss observers, matched life controls, rubric and studies.
- CLI/MCP, Claude/reference/ASSAY/OpenHands bindings, OpenEnv/Inspect faces,
  Harbor packaging, null baselines, goals and preregistration templates.

The [coverage report](docs/implementation-status.md) maps these to implementation
and remaining validation. Each subsystem's operating contract is linked there.
The [verification record](docs/verification.md) lists the checks completed here
and the operating evidence still required.

## Research configuration

Development mode exercises later-stage software locally and refuses production
external writes. Research runs start in `preflight`, withhold the agent token,
and require evidence for that exact configuration before activation. The registry
enforces shakedown → stage 1, positive R1 → stage 2, and R5/L4/IRB → stage 3.
Fixtures cannot unlock research gates.

| Variable | Purpose |
| --- | --- |
| `WORLD_CONTROL_TOKEN` | Trusted control authorization |
| `WORLD_BRAVE_API_KEY` | Brave search, declared $0.005 per attempted provider call |
| `WORLD_OPENAI_API_KEY`, `WORLD_ANTHROPIC_API_KEY` | World-held model authentication |
| `WORLD_OPENAI_UPSTREAM`, `WORLD_ANTHROPIC_UPSTREAM` | Trusted provider roots |
| `WORLD_PRICE_TABLE`, `WORLD_POLICY_FILE` | Reviewed price table and gateway policy |
| `WORLD_DOH_URL` | Optional HTTPS DNS; public-address validation still applies |
| `WORLD_IDENTITY_URL` | Public identity for signed HTTP requests |
| `WORLD_CHROMIUM_EXECUTABLE` | Sandbox-capable system browser executable |
| `WORLD_DATA_DIR` | Private world storage, default `data/` |

Configure secrets in environment settings or the vault, never in run JSON,
chat, logs or saved startup instructions. Model snapshots must occur in
`policy/prices.json`; review its declared rates against provider billing before
paid research. Ambiguous charges retain their hold and taint/freeze the run.

No paid experiment, scientific pilot, 14-day life, day-30 reading or 90-day
company study has completed here. The heuristic injection scanner is explicitly
uncalibrated; category/terms tables need operator review. A real native Claude
Code 2.1.308 fixture pilot passed with two concurrent MCP fetches and reconciled
usage; paid Claude studies and native Harbor/OpenHands pilots remain unrun.
G/V/J/S remain separate, and missing evidence is unmeasured rather than zero.

## Validate

```bash
uv lock --check
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -q
```

Tests exercise real application/socket paths with deterministic upstreams,
budget races, streaming, cancellation, TLS/proxy policy, archives, life/service
workflows and metered research child runs. The local smoke checks a running
process, agent capability, record/ledger, snapshot and export. Deployment tests
also check real fenced egress denial; they are not scientific gate evidence.

Optional explicit checks include `tests/native_claude_smoke.py` (installed native
Claude, entirely local fixtures) and the Docker skill checks in
`tests/test_skill_tests.py` with `WORLD_RUN_DOCKER_SKILL_TESTS=1`. Skill execution
requires a separately configured trusted worker and installed pinned image; the
production deployment never mounts the Docker daemon into the world or agent.
