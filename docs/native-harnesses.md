# Managed native harnesses

The operator application's Harness Library manages Claude Code and Codex through
`HarnessService` in `worldd/harnesses.py`. The supported versions are Claude Code
2.1.308 and Codex 0.163.0. Discovery executes each client's bounded `--version`
command. A different detected version is displayed but does not become ready to
launch. Trusted server configuration can select an existing executable through
`WORLD_CLAUDE_CLI` or `WORLD_CODEX_CLI`; executable paths are never UI inputs.

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
derived from actual native status commands. Subscription model cost remains
unmeasured, and these cooperative development sessions are not research cells.
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
