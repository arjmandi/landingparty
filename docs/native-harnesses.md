# Managed native harnesses

The operator application's Harnesses page manages Claude Code and Codex through
`HarnessService` in `worldd/harnesses.py`. The supported versions are Claude Code
2.1.308 and Codex 0.163.0. Discovery executes each client's bounded `--version`
command. A different detected version is displayed but does not become ready to
launch. Trusted server configuration can select an existing executable through
`WORLD_CLAUDE_CLI` or `WORLD_CODEX_CLI`; executable paths are never UI inputs.

## Kinds, connections, models and effort

A harness manifest (`HarnessManifest` in `worldd/harness_config.py`) is
code-owned. **Native** harnesses (Claude Code, Codex) ship with the world at
their pins; an **add-on** is a trusted developer's registry entry. Neither can
be supplied over HTTP or the UI. Each manifest names its `provider` (whose
models and API key it uses), the effort levels its client accepts, and the
client stream a transcript meter reads, once one has been verified (Claude
Code's `stream-json` today).

In the Docker image, native harnesses are built in from the committed
lockfiles in `deploy/harnesses/` and found through `WORLD_CLAUDE_CLI` and
`WORLD_CODEX_CLI`; the install action below is for running `worldd` directly
and for add-ons.

**Codex on a subscription** stays unmeasured, and a Codex life needs an API key.
`codex exec --json` reports usage once per turn, in `turn.completed`, without the
model's name, and a session is one turn: the world would see a session's spend
only after it ended, so it could not strand a life before the session overspent
its balance. Codex's own session log keeps no per-call usage either.

A harness pays for its model calls through one of two **connections**, both
set up on the harness's own card:

- **API key** — the provider's key, shared by every use of that provider in
  the instance (harnesses, reflection, judges). The lane meters every call.
  An API key is a provider credential, not a harness.
- **Subscription account** — the harness's own signed-in account. Its model
  spend is estimated from the client's stream at reviewed API prices where the
  stream is verified, and is unmeasured otherwise.

Each connection offers the models it actually serves, crossed with the
reviewed price table (`policy/prices.json`, with labels and effort levels in
`policy/models.json`): an API key lists what its provider served at its last
validation (`/v1/models`); a Codex account lists its own catalogue from its
profile's `models_cache.json`; a Claude subscription, which has no list, is
offered the reviewed catalogue, and a model it does not serve holds the life
(see [lives](lives.md)). A served model with no reviewed price is listed but
cannot be chosen where spend must be priced. Only exact model identifiers are
accepted; aliases and "account default" are not.

**Effort** (`RunConfig.effort`) is a registered condition passed to the
client as chosen: `--effort` for Claude Code and `model_reasoning_effort` for
Codex. Leaving it to the model records the model's default explicitly. It
joins the run's `runtime_protocol_id` only when set.

## Install and connect

The explicit Install action requires Node.js and npm on the server. It installs
one fixed, pinned package from the public npm registry into private instance
storage. Viewing the library and starting a run never install packages. Package
lifecycle scripts are disabled. Claude's included platform-native executable is
selected only after checking its parent and optional package names, exact
versions, executable bit, and containment within that installation. Unsupported
platforms or failed verification leave installation failed. Progress and errors
are persisted; interrupted installations are marked interrupted on recovery.

API-key runs send model requests through the World lane. Real provider keys stay
with World; the process receives its scoped run capability. Codex's native
Responses requests omit an output token limit, so the lane adds the run's frozen
limit only after verifying an active, owned Codex API process and its selected
model. Explicit client limits and unmanaged clients retain normal validation.

Subscription connections use official CLI login flows in separate private
profiles, implemented by `HarnessAuthService`. Subscription credentials are not
copied into the settings API or converted into API keys. Connection status is
derived from actual native status commands. Subscription model cost is
estimated where the client's stream is verified (Claude Code) and unmeasured
otherwise, and these cooperative development sessions are not research cells.
No authentication or model call starts merely from library discovery.

After choosing **Connect subscription**, open the **Open provider sign-in**
link in the app. A server or Docker container cannot open your local browser
for you. Claude's confirmation-code input belongs to that provider flow;
paste a code only after completing the linked official authorization page.
The supported Claude client uses `https://claude.com/cai/oauth/authorize`;
the service validates that exact endpoint before exposing the link.

## Process and evidence lifecycle

Launch records the actual version, model, billing mode, prompt hash, and factual
host workspace mapping. The mission preamble remains unchanged. World MCP
provides the run's current tool surface; native web tools are disabled. Codex
runs with `--no-daemon`, a dedicated process group, and fixed configuration.

The runner follows actual subprocess state and exit codes. It drains bounded,
credential-sanitized stdout and stderr into stable cursor logs when model capture
is enabled, records transcript hashes before sealing, and stops owned descendants
when the process exits or the operator stops the run. Recovery verifies process
identity before stopping a prior process; interrupted sessions never acquire an
invented successful exit code. Freezing World pauses the owned process.

Host-native runs share the server's OS user and are cooperative. Their separate
profiles and filtered environment do not prevent filesystem access to that
user's files. See [the operator application](product-experience.md) for this
deployment boundary. A trusted developer can extend the Python manifest registry;
the HTTP/UI surface cannot supply packages, launchers, shell arguments, or paths.

`tests/test_harnesses.py` exercises real subprocess exit, stop, recovery, bounded
capture, install failures, and ownership checks. The explicitly invoked
`tests/native_harness_runner_smoke.py` additionally runs actual supported CLIs
against local provider and website fixtures, including a World MCP fetch and
sealed ledger verification. It performs no paid provider calls and supplies no
scientific gate evidence or proof of an operator's subscription entitlement.
