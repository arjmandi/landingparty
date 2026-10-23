# Landing Party

A self-hosted world for Claude Code and Codex: give an agent a goal and a budget,
follow its recorded work, and handle the specific asks it sends back. The Studio
provides a harness library, provider connections, guided run creation, timelines,
beliefs, asks, footprint and stop controls at one URL.

There is one trusted operator per instance. You do not need an account on the
Landing Party website. Start with the [getting-started guide](GETTING_STARTED.md)
for the complete local or VPS workflow.

## Start the Studio

Use Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js 22+ with npm on
the machine running the world. The Library uses npm to install supported native
clients; you do not need to install Codex or Claude globally first.

```bash
git clone https://github.com/arjmandi/landingparty.git
cd landingparty
git switch codex/implement-landing-party
uv sync --frozen --extra dev
uv run --no-sync worldd --data-dir data
```

Studio is currently on `codex/implement-landing-party`; use `main` after that
development branch is merged, or a published release tag that includes it.

Open `http://127.0.0.1:7777/`. Then:

1. In **Library**, install Claude Code or Codex.
2. Choose **Connect subscription** and complete the official provider sign-in,
   or add your provider API key in **Settings**. Claude's manual confirmation-code
   flow also works when the world runs on a VPS.
3. Start a run with the chosen harness, connection, goal, dollar allowance and
   time limit.
4. Follow its record, beliefs, asks and footprint. Respond through explicit ask
   controls and use **Stop run** to end it. There is no ad hoc operator chat.

Studio's native launch is **cooperative**: the agent runs as the same OS user as
`worldd` and can access that user's private files, including world storage.
Separate client profiles and environment filtering do not isolate those files.
Use a dedicated/disposable machine or container with no unrelated secrets. The
[advanced fenced deployment](deploy/README.md) uses separate world/agent users
and network isolation; the Studio host-native launcher does not create it.

Subscription model dollars are **unmeasured**, not zero. In subscription mode,
the run's dollar allowance covers recorded world-tool charges only; your provider
plan has its own limits. API-key mode routes model calls through the metered
lane. Both modes require actual provider access; software fixtures do not supply
it or pass research gates.

The Studio is local-only by default. For a VPS, configure `WORLD_CONTROL_TOKEN`
and a TLS reverse proxy; sign in to the same-origin Studio once. See
[single-origin hosting](docs/studio-origin.md) and the
[operator workflow](docs/product-experience.md). Keep the recorded TCP proxy
private; it is not another browser frontend.

## Advanced clients and development

The default HTTP origin exposes operator controls at `/`, run-scoped tools at
`/agent`, and the model lane at `/model`. `/bridge` retains the advanced ticket,
credential-deposit and browser-takeover console. Manual CLI and MCP instructions
are in the [advanced part of the guide](GETTING_STARTED.md#advanced-cli-and-mcp).
`--legacy-listeners` restores the separate 7777/7778/8081 HTTP listeners.

A free local check against the default Studio needs no model account:

```bash
# In another terminal, from the checkout:
uv run --no-sync python scripts/smoke.py \
  --url http://127.0.0.1:7777 \
  --agent-url http://127.0.0.1:7777/agent
```

If the server has a control token configured, provide the same
`WORLD_CONTROL_TOKEN` in this terminal. The check makes no paid or external
requests and verifies scoped tools, the record, ledger, seal and export.

Optional `full` dependencies add Playwright, PDF parsing and extractors;
`standards` adds pinned OpenEnv, Inspect, Harbor and OpenHands SDKs. Browser use
needs a sandbox-capable Chromium and `certutil`; see [deployment](deploy/README.md).
Non-WAV audio needs `ffprobe` from FFmpeg. These are separate from installing a
native client through the Library.

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
The [runtime audit](docs/runtime-audit.md) records the additional boundary fixes.
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
| `WORLD_EXA_API_KEY` | Alternate Exa neural search; select `search_provider: exa`, declared $0.005 per attempted call |
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
usage. Native OpenEnv, Inspect and OpenHands SDK checks and an actual fenced
Harbor environment have also run against local fixtures. Paid provider studies
and real external venue cohorts remain unrun.
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
