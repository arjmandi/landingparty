# Getting started with Landing Party

Landing Party runs a recorded, budgeted environment around an agent. You start
the world process, create a run, and connect a client such as plain **Codex CLI**
or **Claude Code**. The agent gets that run's tools and capability; you retain
the controls, provider credentials, ledger and reports.

This guide covers self-hosting from a clone when the repository becomes public.
The website is not required for local use. Start with the free local smoke check,
then choose **one** client below. The examples use development mode; the final
section explains the additional requirements for live research.

## 1. Install the prerequisites

Use Linux or macOS with Bash, Git, Python 3.12 or newer, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). On Windows, use
WSL2 with a Linux shell. Linux with Docker is the validated path for fenced
deployment and sandboxed browser operation.

For the native clients, install Node.js 22 or newer and the CLI you want:

```bash
npm install -g @openai/codex
# Or:
npm install -g @anthropic-ai/claude-code
```

The [Codex installation guide](https://developers.openai.com/codex/cli) and
[Claude Code setup guide](https://code.claude.com/docs/en/setup) describe their
other supported installers. Native command syntax in this guide was checked
with Codex CLI 0.163.0 and Claude Code 2.1.308. Check your installed version with
`codex --version` or `claude --version`.

You do **not** need Docker, a browser, an account on the website, or an API key
for the first smoke check. You need provider API access for model calls through
the metered lane. An ordinary Codex/Claude subscription is a separate billing
path and does not supply the world server with an API key.

## 2. Clone and install

Until the implementation is merged into `main`, use its development branch:

```bash
git clone --branch codex/implement-landing-party \
  https://github.com/arjmandi/landingparty.git
cd landingparty
export LANDING_PARTY_ROOT="$PWD"

uv sync --frozen --extra dev
source .venv/bin/activate
uv lock --check
```

For a published release, use its announced tag or branch instead. Commands in
this guide assume the virtual environment is active. Reopen it with
`source .venv/bin/activate` in each new terminal.

The basic install includes the server, CLI, MCP bridge and analysis tools.
Optional dependencies are available when needed:

| Installation | Adds |
| --- | --- |
| `uv sync --frozen --extra dev --extra full` | Playwright, local PDF parsing and additional extractors |
| `uv sync --frozen --all-extras` | All of the above plus the pinned OpenEnv, Inspect, Harbor and OpenHands SDKs |

The `full` extra alone does not configure a working Chromium sandbox. For
browser use, follow [fenced deployment](deploy/README.md). Non-WAV audio also
needs `ffprobe` from FFmpeg; the world container includes it.

## 3. Start the world and run the free check

In **terminal A**, from the checkout, create a private control token and start
the server:

```bash
umask 077
mkdir -p data
python - <<'PY'
import secrets
from pathlib import Path

path = Path("data/control-token")
if not path.exists():
    with path.open("x") as target:
        target.write(secrets.token_urlsafe(32))
    path.chmod(0o600)
PY

export WORLD_CONTROL_TOKEN="$(cat data/control-token)"
worldd --data-dir "$LANDING_PARTY_ROOT/data"
```

Leave this terminal running. `data/` is private runtime storage and is ignored
by Git. Keep this directory and the control token outside the agent's accessible
filesystem when deploying with isolation.

In **terminal B**, open the same checkout and run:

```bash
export LANDING_PARTY_ROOT="$PWD"
source .venv/bin/activate
export WORLD_CONTROL_TOKEN="$(cat data/control-token)"

lp health
python scripts/smoke.py
```

The smoke script should print `"status": "passed"`. It creates and seals its
own run, checks the scoped agent capability, delivers an artifact, records an
ask, checks private-address denial, and verifies the ledger, snapshot and
export. It makes no paid or external requests.

The default listeners are:

| Address | Used by |
| --- | --- |
| `http://127.0.0.1:7777` | You: `lp`, reports and the `/bridge` console |
| `http://127.0.0.1:7778` | Agents: scoped tools, inbox, manifest and status |
| `http://127.0.0.1:8081` | Clients: metered OpenAI/Anthropic model requests |
| `http://127.0.0.1:8080` | Recorded HTTP/HTTPS proxy, when configured for a run |

Open `http://127.0.0.1:7777/bridge` for the researcher console; enter the control
token locally when prompted. Keep these listeners on loopback for this setup.
The optional public identity/incident listener is separate; it is not needed
to connect a client.

## 4. Configure a provider before calling a model

Stop the server in terminal A with Ctrl-C. Put provider keys in **terminal A
only**, or supply them through your deployment's secret manager.

For Codex, enter an OpenAI API key without putting its value in shell history:

```bash
read -r -s -p 'OpenAI API key: ' WORLD_OPENAI_API_KEY
printf '\n'
export WORLD_OPENAI_API_KEY
```

For Claude Code, use an Anthropic API key instead:

```bash
read -r -s -p 'Anthropic API key: ' WORLD_ANTHROPIC_API_KEY
printf '\n'
export WORLD_ANTHROPIC_API_KEY
```

Only configure the provider you plan to use. Brave search optionally uses
`WORLD_BRAVE_API_KEY`; Exa uses `WORLD_EXA_API_KEY` with `search_provider: exa` in
the run configuration. These keys also belong only to the world process.
Direct URL fetching does not need a search-provider key.

Review [policy/prices.json](policy/prices.json) against the provider's current
documentation. Its entries are declared example snapshot rates, not a current
price feed. You can maintain your reviewed table privately:

```bash
cp -n policy/prices.json data/prices.json
# Edit data/prices.json before making paid calls.
export WORLD_PRICE_TABLE="$LANDING_PARTY_ROOT/data/prices.json"
```

For each exact model ID, retain the table's schema: provider, input/output and
cache rates per million tokens, context window, maximum output tokens, and
source URL. Add the exact **Codex-compatible OpenAI model** you intend to use;
the bundled OpenAI examples do not guarantee compatibility with current Codex.
For Claude, choose an exact Anthropic model available to your account. Avoid
aliases such as `sonnet` or `latest`: an unpriced model is refused.

Restart the server in terminal A with the same command:

```bash
worldd --data-dir "$LANDING_PARTY_ROOT/data"
```

The model lane reads provider roots and keys from this process. Default upstream
roots are `https://api.openai.com` and `https://api.anthropic.com`. If you use
`WORLD_OPENAI_UPSTREAM` or `WORLD_ANTHROPIC_UPSTREAM`, supply the root **without
`/v1`**. Review compatibility and prices for that upstream too.

## 5. Create a fresh client run

Continue in terminal B. Choose the exact model ID you reviewed above, and label
the client/version you will use:

```bash
read -r -p 'Exact priced model ID: ' WORLD_MODEL
export WORLD_MODEL

# Pick one, using your actual installed version:
export WORLD_HARNESS="$(codex --version)"
# export WORLD_HARNESS="$(claude --version)"

export SESSION_DIR="$(mktemp -d "$LANDING_PARTY_ROOT/data/session.XXXXXX")"
python - <<'PY'
import json
import os
from pathlib import Path

config = {
    "goal": "Inspect the world tools, call status, and deliver a short note. Do not contact anyone or publish anything.",
    "family": "F7",
    "budget": {"usd": 2, "wall_s": 1800},
    "packs": ["deliver"],
    "harness_id": os.environ["WORLD_HARNESS"],
    "model_snapshot_id": os.environ["WORLD_MODEL"],
    "capture_model_io": True,
    "reply_policy": "silent",
    "isolation_profile": "cooperative",
    "research_mode": "development",
}
Path(os.environ["SESSION_DIR"], "config.json").write_text(json.dumps(config, indent=2))
PY

lp energize "$SESSION_DIR/config.json" > "$SESSION_DIR/run.json"
export WORLD_RUN_ID="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$SESSION_DIR/run.json")"
export WORLD_RUN_TOKEN="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_token"])' "$SESSION_DIR/run.json")"
export WORLD_WORKSPACE="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["workspace_path"])' "$SESSION_DIR/run.json")"
export LANDING_PARTY_PYTHON="$LANDING_PARTY_ROOT/.venv/bin/python"

python - <<'PY'
import json
import os
from pathlib import Path

session = Path(os.environ["SESSION_DIR"])
run = json.loads((session / "run.json").read_text())
(session / "briefing.txt").write_text(run["preamble"])
PY
```

This is a real model-spending cap of **$2**, with a 30-minute wall limit. Paid
requests begin when you launch a client. The lane reserves each call's maximum
possible cost, so a large output allowance can be refused before the $2 is
spent. Review the configured token limits rather than raising the budget
without checking why.

`run.json` includes a private, run-scoped capability. Do not commit or share it.
The saved briefing is the actual world-generated preamble; give it to the
client verbatim. Use a **fresh run** for each client or retry after a sealed run.
These local examples use the returned workspace directory directly; they do
not create the `/work` and read-only `/world` mounts of the fenced deployment.

## 6A. Connect plain Claude Code

No custom Claude SDK is required. Generate the repository's existing MCP and
settings files in terminal B:

```bash
python adapters/claude-code/configure.py \
  --run-id "$WORLD_RUN_ID" \
  --output "$SESSION_DIR/claude"
```

Then launch the ordinary CLI in the run workspace:

```bash
(
  unset WORLD_CONTROL_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
  export WORLD_BASE_URL=http://127.0.0.1:7778
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8081/anthropic
  export ANTHROPIC_AUTH_TOKEN="$WORLD_RUN_TOKEN"
  export ANTHROPIC_CUSTOM_HEADERS="X-World-Run-Id: $WORLD_RUN_ID"
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  cd "$WORLD_WORKSPACE"

  claude --model "$WORLD_MODEL" \
    --strict-mcp-config \
    --mcp-config "$SESSION_DIR/claude/world.mcp.json" \
    --settings "$SESSION_DIR/claude/world.settings.json" \
    --tools 'Bash,Read,Write,Edit,Glob,Grep' \
    "$(cat "$SESSION_DIR/briefing.txt")"
)
```

Approve the world MCP tools when asked. Use `/mcp` to inspect the connection.
The generated settings disable Claude's built-in `WebSearch`/`WebFetch`; use
world `search`/`fetch` instead. Requests to the Anthropic model lane authenticate
with the run capability, which the world replaces with its own provider key.

For a noninteractive transcript-producing invocation, the existing
[Claude launcher](adapters/claude-code/README.md) is also available. Both paths
use the same native Claude Code executable and MCP bridge.

## 6B. Connect plain Codex CLI

Codex uses the same stdio MCP bridge and the world's OpenAI Responses endpoint.
This command sets the provider and MCP configuration for this invocation,
without writing the capability into `~/.codex/config.toml`:

```bash
(
  unset WORLD_CONTROL_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
  export WORLD_BASE_URL=http://127.0.0.1:7778

  codex --no-daemon --cd "$WORLD_WORKSPACE" --model "$WORLD_MODEL" \
    --sandbox workspace-write \
    -c 'web_search="disabled"' \
    -c 'model_provider="landing_party"' \
    -c 'model_providers.landing_party.name="Landing Party"' \
    -c 'model_providers.landing_party.base_url="http://127.0.0.1:8081/openai/v1"' \
    -c 'model_providers.landing_party.wire_api="responses"' \
    -c 'model_providers.landing_party.env_key="WORLD_RUN_TOKEN"' \
    -c 'model_providers.landing_party.requires_openai_auth=false' \
    -c 'model_providers.landing_party.env_http_headers={"X-World-Run-Id"="WORLD_RUN_ID"}' \
    -c "mcp_servers.world.command=\"$LANDING_PARTY_PYTHON\"" \
    -c 'mcp_servers.world.args=["-m","worldd.mcp"]' \
    -c 'mcp_servers.world.env_vars=["WORLD_RUN_ID","WORLD_RUN_TOKEN","WORLD_BASE_URL"]' \
    "$(cat "$SESSION_DIR/briefing.txt")"
)
```

Use `/mcp` in Codex to inspect the world connection. The custom provider sends
the run token as its bearer credential and the run ID as a header. The world
validates them, charges the correct run, and uses its server-held OpenAI key.
Native hosted web search is disabled because it bypasses the recorded gateway.
For a script, use `codex ... exec --skip-git-repo-check --json` with the same
configuration and briefing; a run workspace need not be a Git repository.

MCP and model routing are separate. Connecting only the MCP bridge while using
your normal Codex login gives you world tools, but model calls then use your
normal billing path and are **outside Landing Party's ledger and dollar cap**.
The custom provider above is the path to use when checking model metering.
The same distinction applies to Claude Code with its ordinary login.

## 7. Inspect, stop and export

Exit the client, then use terminal B with its control token still set. Plain
interactive clients do not automatically seal the world run when you quit:

```bash
lp --url http://127.0.0.1:7777 status "$WORLD_RUN_ID"
lp --url http://127.0.0.1:7777 beam-up "$WORLD_RUN_ID"
lp --url http://127.0.0.1:7777 report "$WORLD_RUN_ID"
lp --url http://127.0.0.1:7777 report "$WORLD_RUN_ID" \
  --format html --output "$SESSION_DIR/report.html"
lp --url http://127.0.0.1:7777 export "$WORLD_RUN_ID" \
  --output "$SESSION_DIR/episode.tar.gz"
```

For an immediate stop, use `lp emergency-beam-out "$WORLD_RUN_ID"`.
Authoritative records, model captures, workspace snapshots and analysis artifacts
live under `data/runs/<run_id>/`. Captures and exports can contain private
material; review them before sharing. Stop the world process with Ctrl-C when
finished. Existing records remain on disk.

An ordinary report is passive. Optional model-based grading, interviews and
probes need a separate explicit measurement budget; see
[research analysis](docs/research.md).

## 8. Move from local setup to live research

The host commands above are **cooperative development**. The client shares your
machine, shell tools can bypass the gateway, and ordinary client sandboxing does
not establish Landing Party's fenced isolation or complete accounting. Keep
provider secrets in terminal A only; if terminal B already contains other
credentials, start a clean client environment. A shared OS user can still read
its own files regardless of environment-variable cleanup.

Before a scientific live run:

1. Deploy the [fenced profile](deploy/README.md): separate world/agent users,
   exact workspace mounts, default-drop agent egress and the recorded proxy.
   Install the chosen native CLI in the reviewed agent image; the minimal image
   does not bundle Codex or Claude Code. Configure the run's proxy capability and
   trust its exact world CA as described there.
2. Verify actual public egress and provider usage/billing with small authorized
   calls. Some cloud proxies reject CONNECT to validated destination IPs; fix
   the deployment's egress rather than disabling destination pinning or TLS.
3. Freeze the model versions, reviewed prices, goals/hidden banks and expert
   calibration material; register the study and explicit budgets.
4. Bind the actual harness and complete configuration-bound preflight. Research
   runs withhold the agent token until activation. Stage gates require actual
   prior evidence; development fixtures cannot unlock them.
5. Begin the design's Stage-0 shakedown. Later stages additionally need R1/R5/L4
   results, owned identities/venues, applicable consent/IRB decisions and actual
   elapsed life/afterlife windows.

These are the next live-research steps, not actions performed by this guide.
The [implementation status](docs/implementation-status.md) and
[verification record](docs/verification.md) distinguish implemented software
from completed experiments.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `worldd` or `world-mcp` not found | Activate `.venv`; the client MCP command uses its absolute Python path. |
| Port already in use | Stop your own previous world process or use another host. The authenticated `lp` CLI currently expects the control listener on loopback port 7777. |
| Control request returns 401 | Terminal B must have the same `WORLD_CONTROL_TOKEN` as terminal A. |
| MCP fails or returns 401 | Use port 7778, the current run ID and its token. Restart the client after changing its environment; sealed capabilities are revoked. |
| `unpriced_model` | The actual requested model must exactly match an entry for its provider in the server's active price table. Restart after changing the table. |
| `budget_exhausted` before much is spent | The lane reserves the call's maximum possible cost; check output limits and unresolved holds. |
| `server_side_web_tool` | Disable the client's built-in hosted search/fetch and use the world MCP tools. |
| Tools work but model dollars stay zero | Check the client's custom model endpoint; an ordinary subscription/provider connection bypasses the model lane. |
| Fetch is blocked | Inspect the typed reason: robots/terms, private destination, missing search key or egress failure. A blocked observation is not permission to bypass policy. |
| Chromium cannot start | Use the documented sandbox-capable container; the basic install does not supply it. |
| Run is already sealed | The deadline, budget or a stop ended it. Create a new run and reconnect with its new capability. |

For development checks, run `ruff check .`, `ruff format --check .` and
`pytest -q`. The normal suite needs no provider keys; eight optional real-Docker
skill cases are skipped unless explicitly configured as described in
[skill checks](docs/skill-tests.md).
