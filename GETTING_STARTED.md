# Getting started with Landing Party

The Studio is the main way to use Landing Party. Start one world process, open
one URL, install a native harness through the Library, connect a provider, and
start a run. Follow its timeline, beliefs, asks and footprint from the same app.
There is one trusted operator per instance; no Landing Party website account or
hosted service is required.

## Before you start

Use a **dedicated/disposable machine or container** for this quickstart. Studio's
host-native agents run cooperatively as the **same OS user as `worldd`**. They
can read that user's private files, including world storage and native login
profiles. Separate profiles and filtered environment variables do not provide
filesystem or network isolation. Keep unrelated secrets off that environment.
The [fenced deployment](deploy/README.md) is a separate advanced setup with
world/agent user separation, controlled mounts and restricted egress.

Install these on the machine that will run `worldd`:

- Python 3.12+, Git, Bash and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Node.js 22+ and npm for the Library's native-client installer.
- A browser on your own computer to use Studio and complete provider sign-in.

Linux is the validated host-native path. Use WSL2 on Windows. Docker is needed
for the fenced deployment and some optional browser/skill features; it is not
required just to start Studio. A provider API account or a supported Claude/
Codex subscription is needed to run a model, but not to open the app or execute
the free smoke check below.

## 1. Clone and install

```bash
git clone https://github.com/arjmandi/landingparty.git
cd landingparty
export LANDING_PARTY_ROOT="$PWD"
uv sync --frozen --extra dev
uv lock --check
```

The default checkout is `main`, which includes Studio. For a published release,
use its tag.

The basic environment includes the server, CLI, MCP bridge and analysis tools;
it does not install a native model client yet.

Optional dependencies are separate:

| Installation | Adds |
| --- | --- |
| `uv sync --frozen --extra dev --extra full` | Playwright, PDF parsing and extractors |
| `uv sync --frozen --all-extras` | Those packages plus pinned OpenEnv, Inspect, Harbor and OpenHands SDKs |

A Python Playwright package alone does not configure a sandbox-capable browser.
See [deployment](deploy/README.md) for browser prerequisites. Non-WAV audio
inspection also requires `ffprobe` from FFmpeg.

## 2. Open Studio

From the checkout:

```bash
uv run --no-sync worldd --data-dir "$LANDING_PARTY_ROOT/data"
```

Leave it running and open `http://127.0.0.1:7777/`. Local loopback development
without `WORLD_CONTROL_TOKEN` opens directly. If a control token is already
configured on the server, enter it in Studio's connection screen. This is a
single-operator instance, not a multi-user account system.

`data/` is private persistent runtime storage, ignored by Git. Keep it between
restarts to retain runs, settings and managed native profiles. The app's default
HTTP layout is:

| Address/path | Purpose |
| --- | --- |
| `http://127.0.0.1:7777/` | Studio and trusted operator API |
| `/agent/v1/*` | Tools, manifest, inbox and status using a run capability |
| `/model/*` | Metered OpenAI/Anthropic model endpoints using a run capability |
| `/bridge` | Advanced asks, credential deposits and browser takeover |
| `/public/*` (when `--public` is enabled) | Optional identity/incident boundary |

The recorded proxy remains a private TCP service, normally on loopback port
8080. It is not another browser frontend. See [single-origin hosting](docs/studio-origin.md)
for deployment details and [the operator workflow](docs/product-experience.md)
for how the panels behave.

## 3. Install a harness in Library

Open **Library** and choose Claude Code or Codex. Click **Install client** if it
is not installed. Installation uses the supported pinned npm package in this
instance's private tools directory; it does not need a global npm installation
or `sudo`. Wait for installation to finish and readiness to refresh.

Node.js and npm must be available to the **server process**, including on a VPS
or in a container. If you installed them after starting `worldd`, restart the
server so its PATH contains them, then retry. An existing supported CLI found
by the server can also be used. The code-owned library currently pins Claude
Code 2.1.308 and Codex CLI 0.163.0.

## 4. Choose how to connect

### Use your provider subscription

In the harness card, choose **Connect subscription**. This invokes the official
native client's sign-in flow; it does not turn an API key into a subscription.
Wait for **Open provider sign-in**, then click that link to open the provider
in your browser. The native client runs inside the server or container, so it
does not open a terminal or browser window on your computer automatically.

- **Codex:** open the official provider device sign-in URL, enter the displayed
  device code when requested, and authorize your ChatGPT account.
- **Claude Code:** open the official Claude sign-in URL and authorize your
  subscription. If the provider displays a confirmation code, paste it into
  **Provider confirmation code** in Studio and submit it. This manual-code path
  works when the native client is on a VPS and a browser callback cannot reach
  that server.

Return to the Library and click **Check connection** if needed. Wait for the
state to become connected before starting a run. The app checks the official
CLI's local login status; a URL being displayed is not successful authentication.
Use **Cancel sign-in** for an interrupted flow, or **Disconnect** to remove that
instance's connection. Studio manages its own native profiles; a login in your
ordinary terminal profile does not automatically connect this instance.

Subscription model dollars are **unmeasured**, not zero. In this mode, the dollar
allowance covers recorded **world-tool charges only**. Provider plan limits and
quotas remain separate, and model work can continue without consuming that world
allowance. Subscription runs are cooperative development runs and cannot be
used as metered research cells.

### Use an API key

Open **Settings**, add the corresponding OpenAI or Anthropic API key, and save.
Keys are write-only in the interface; a configured state does not reveal the
saved value. Use **Check API access** when available to check provider access.
Then return to the harness card's API-key connection.

Select an exact priced model offered by the instance. API mode routes native
client model requests through Landing Party's metered lane; model and tool
charges count against the run's dollar cap. A subscription login does not supply
an API key or API credit. Review the active [price table](policy/prices.json)
against provider pricing before paid research; an unpriced model is refused.

Optional Brave/Exa search keys also belong in Settings or the world process's
secure environment. Direct URL fetching does not need a search-provider key.
Do not put provider keys or native login credentials in goals or workspaces.
Remember that this quickstart's same-OS-user boundary still permits file access.

## 5. Beam down, follow and beam up a life or a run

Click **Beam down with this connection** or **Beam down**. The dialog opens on **A life**:
select the harness and an API connection, set its starting balance and session
length, and leave the objective empty unless you want one. The balance pays for
its model and tool use. When it stops without sleeping it rests until an event or
a daily check-in; when its balance cannot cover a model call it is stranded until
a deposit arrives. Low money never ends a life: only **Beam up**, or your
confirmation of an objective it claims, does. Record income it earned elsewhere
with **Record a deposit** on the life.

Choose **A single run** for one session with a dollar allowance and time limit; a
subscription connection always starts a single run, because its model spend
cannot be metered into a balance. An objective for a first run can be:

> Inspect the world tools, call status, and deliver a short note. Do not contact
> anyone or publish anything.

Starting the run launches the selected native client with its actual world
briefing, workspace and scoped tools. It can make provider requests immediately.
For API mode, choose a modest allowance and check the selected model's maximum
output reservation if a call is refused before much has been spent.

Use **Lives & runs** for the actual state, balance or spending, the ship's log
and the beam-up control. **Beliefs** shows the personal log and its evidence; **Footprint**
shows already recorded work and observations. **Asks** lets you inspect and
respond to a specific request. Secure credential handoffs and browser takeover
continue in the advanced bridge when indicated. These are explicit actions;
there is no general operator chat that redirects a run silently.

Opening a panel does not call a model, consume the agent's inbox or create a
belief. Missing records and prices remain unavailable. Use **Beam up** and
follow its state while work drains and the run or life is sealed; **End session**
on one of a life's sessions only rests the life. Closing the browser
alone does not stop the server or its agent. Existing records remain on disk
when you stop `worldd` with Ctrl-C.

## 6. Use a VPS or cloud host

Keep the same one-origin layout. Configure `WORLD_CONTROL_TOKEN` in the server's
secure environment before starting it and put a TLS reverse proxy in front of
the loopback HTTP listener. Open the resulting HTTPS URL and enter the token
once; Studio uses an HttpOnly, SameSite Strict session cookie. There is no
separate website signup.

A non-loopback `worldd` bind is refused without a control token. Do not publish
the proxy TCP port or expose unrelated server files to a native agent. Use a
dedicated/disposable environment here too. See [single-origin hosting](docs/studio-origin.md)
for authentication, session expiry, routing and legacy-listener behavior.

## Advanced CLI and MCP

Studio does this wiring for ordinary runs. The following paths are for scripted
checks, manual native clients and integrations. Below, **terminal A** is the
terminal running `worldd`; **terminal B** is a second terminal for these commands.
Open terminal B in the checkout and activate the Python environment:

```bash
export LANDING_PARTY_ROOT="$PWD"
source .venv/bin/activate
```

A free check against the default Studio makes no model or external requests:

```bash
# Export the same WORLD_CONTROL_TOKEN here if the server has one configured.
lp health
python scripts/smoke.py \
  --url http://127.0.0.1:7777 \
  --agent-url http://127.0.0.1:7777/agent
```

It should print `"status": "passed"`, after verifying a run capability, tools,
private-destination denial, seal, ledger, snapshot and export. To use older
clients that expect separate listeners, start the server with:

```bash
worldd --data-dir "$LANDING_PARTY_ROOT/data" --legacy-listeners
```

That explicitly restores control port 7777, agent port 7778 and model port 8081.
The manual examples below use the default Studio's `/agent` and `/model` paths
instead. Do not mix those layouts accidentally.

For a manual client, install the corresponding official CLI yourself if it is
not on PATH (the Library's private installation need not be on your shell PATH):

```bash
npm install -g @openai/codex@0.163.0
# Or:
npm install -g @anthropic-ai/claude-code@2.1.308
```

The [Codex CLI guide](https://developers.openai.com/codex/cli) and
[Claude Code setup guide](https://code.claude.com/docs/en/setup) describe the
official installers. Keep manual clients in the same disposable environment.

### Configure a provider manually (API mode)

These manual examples use API metering. Skip the environment-key steps if you
already configured this provider in Studio Settings. Otherwise stop the server
in terminal A with Ctrl-C and put provider keys in **terminal A only**, or use
your deployment's secret manager.

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
uv run --no-sync worldd --data-dir "$LANDING_PARTY_ROOT/data"
```

The model lane reads provider roots and keys from this process. Default upstream
roots are `https://api.openai.com` and `https://api.anthropic.com`. If you use
`WORLD_OPENAI_UPSTREAM` or `WORLD_ANTHROPIC_UPSTREAM`, supply the root **without
`/v1`**. Review compatibility and prices for that upstream too.

### Create a fresh manual client run

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

### Connect plain Claude Code manually

No custom Claude SDK is required. Generate the repository's existing MCP and
settings files in terminal B:

```bash
python adapters/claude-code/configure.py \
  --run-id "$WORLD_RUN_ID" \
  --world-url http://127.0.0.1:7777/agent \
  --lane-url http://127.0.0.1:7777/model/anthropic \
  --output "$SESSION_DIR/claude"
```

Then launch the ordinary CLI in the run workspace:

```bash
(
  unset WORLD_CONTROL_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
  export WORLD_BASE_URL=http://127.0.0.1:7777/agent
  export ANTHROPIC_BASE_URL=http://127.0.0.1:7777/model/anthropic
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

### Connect plain Codex CLI manually

Codex uses the same stdio MCP bridge and the world's OpenAI Responses endpoint.
This command sets the provider and MCP configuration for this invocation,
without writing the capability into `~/.codex/config.toml`:

```bash
(
  unset WORLD_CONTROL_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
  export WORLD_BASE_URL=http://127.0.0.1:7777/agent

  codex --no-daemon --cd "$WORLD_WORKSPACE" --model "$WORLD_MODEL" \
    --sandbox workspace-write \
    -c 'web_search="disabled"' \
    -c 'model_provider="landing_party"' \
    -c 'model_providers.landing_party.name="Landing Party"' \
    -c 'model_providers.landing_party.base_url="http://127.0.0.1:7777/model/openai/v1"' \
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

### Inspect, stop and export from the CLI

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

## Move from local setup to live research

The host commands above are **cooperative development**. The client shares your
machine, shell tools can bypass the gateway, and ordinary client sandboxing does
not establish Landing Party's fenced isolation or complete accounting. Filtering
terminal B's environment does not isolate the filesystem: a native
agent running as the world's OS user can still read world storage and that user's
other private files. Use the dedicated/disposable development environment
described above, and use separate users and mounts for fenced deployment.

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
| Library cannot install a client | Node.js 22+ and npm must be on the server's PATH; restart after installing them. Inspect the actual installation result and retry. |
| Subscription is not connected | Complete the official provider flow and use Check connection; an API-key login is not a subscription login. |
| Claude sign-in on a VPS waits for a code | Complete provider authorization, then submit its confirmation code in the Library. |
| A connection is busy | One native profile can have only one active run; stop its current run first. |
| Studio asks for a token / returns 401 | Use the instance's configured control token; an agent capability or provider key does not grant operator access. |
| MCP returns 401 | Use `/agent`, the current run ID and capability. In explicit legacy mode use port 7778. Sealed capabilities are revoked. |
| `unpriced_model` | API mode requires the exact model ID in the instance's active price table. |
| Model dollars are unavailable | Expected in subscription mode; only world-tool dollars are metered there. For manual API clients verify `/model` routing. |
| `budget_exhausted` before much is spent | The lane reserves the call's maximum possible cost; check output limits and unresolved holds. |
| `server_side_web_tool` | Disable native hosted search/fetch in a manual client and use world MCP tools. |
| Fetch is blocked | Inspect the typed robots/terms, private-destination or egress reason; use an allowed alternative. |
| Chromium cannot start | The base install does not supply the documented sandbox-capable browser setup. |
| Run is sealed | A deadline, budget or stop ended it. Create a fresh run with a new capability. |

Development checks are `uv lock --check`, `ruff check .`, `ruff format --check .`
and `pytest -q`. The ordinary suite uses deterministic provider fixtures;
optional Docker, native-client and browser checks have separate prerequisites.
See the [verification record](docs/verification.md) and [skill checks](docs/skill-tests.md).
