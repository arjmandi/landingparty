# Standards faces

These adapters preserve the core's capabilities and treatment conditions.
They do not create additional permissions or claim the live internet resets.

Install optional native SDKs with `uv sync --extra standards`. Tested versions
are pinned in that extra; importing the base World does not require them.

`faces.openenv.create_openenv_app` exposes the lightweight `/reset`, `/step`, `/state`, and
`/spaces` with typed step input. Reset seals the previous session and starts a
new run. Step is submit plus an optional bounded wait; a remaining Ticket is a
valid observation. Live reward is always `None`. A scorer can supply a replay
reward only when the caller explicitly configured one. The face uses a trusted
control client for creation/seal and a run-scoped agent client for tools.
Run capability values are excluded from observations.

`python -m faces.openenv.native --config goals/14-audit/run.json` starts the
actual openenv-core server on loopback port 8000. Its typed
`LandingPartyEnvironment` and `LandingPartyClient` use the SDK's persistent
WebSocket `/ws` protocol. The action contains `tool`, `params`, and
`wait_seconds`; a fetch ticket can be requested with `params.wait=false`.
Server-side creation/sealing uses the trusted control connection; clients
receive neither its token nor the run capability. The declared harness ID
includes the SDK version. Reset creates another run, including when the same
seed is supplied. It never resets the live internet.

`faces.harbor.build_task` generates a Harbor task directory using the
`landing-party-agent:0.1` image and the external
`deploy/compose.fenced.yml` boundary. It writes task metadata, instruction,
Dockerfile routing, and metadata for a native no-score verifier. A container image or
proxy environment alone does not enforce fencing: launch in the fence network
namespace, inject the runtime run capability, mount only the selected `/world`
and `/work`, and pass the actual egress preflight. The package does not embed
capabilities in images. A Harbor runner that requires a numeric reward must
use `faces.harbor.native:WorldNoScoreVerifier`, whose actual Harbor
`VerifierResult` has `rewards=None`. Harbor's built-in numeric verifier rejects
a null reward file; the fallback `tests/test.sh` therefore fails with an
instruction to select the custom verifier.

`faces.harbor.write_job` writes a native Harbor `JobConfig` selecting
`faces.harbor.native:FencedExistingEnvironment` and the no-score verifier.
Supply the real run ID, existing agent and firewall container IDs, and private
store root. The environment validates their labels, nonroot UID, dropped
capabilities, read-only filesystem, run-scoped mounts, internal network, and
actual IPv4/IPv6 firewall rules through `DockerHarness`. It refuses rebuilds,
root execution, unapproved mounts, and transfers beyond `/work`, `/world`,
`/tmp`, or agent logs; `/world` remains read-only. Stop affects only the bound
agent. Host-side verification seals the run and writes the actual report;
researcher credentials never enter the agent container. Preinstall the chosen
Harbor agent in the registered image, and configure its model/MCP routing;
this adapter does not grant unfenced package-install access.
Bind the trial's actual `agent/` host directory writable at `/logs/agent`
before launch; native Harbor logging requires that exact mount. An optional
verifier-log mount is accepted only read-only, and test/judge banks are never
mounted into the agent.

`faces.inspect.code_scorer` reads sealed report columns without inventing
scores. `inspect_scorer()` creates a native Inspect scorer when the optional
`inspect-ai` SDK is installed; it attaches the report as score metadata and
leaves unavailable numeric values unmeasured. Judge-based scoring belongs to
the post-run analysis pipeline, not an in-run face.

`faces.inspect.native.inspect_task([{data_dir, run_id}, ...])` returns an
actual Inspect `Task` with a passive solver and native scorer. It verifies the
sealed record and ledger, reads code-derived report columns, and makes no
model-generation request. Missing scores stay metadata with `reward=None`.
An explicit rubric callback can return a finite numeric reward only for an
untainted replay episode. Live rubric rewards and corrupt records are refused.

`tests/native_sdk_smoke.py` explicitly exercises actual OpenEnv WebSocket,
Inspect evaluation, Harbor verifier factory, and OpenHands SDK sessions against
a local World fixture. `tests/native_harbor_smoke.py` additionally exercises
the native Harbor environment against real disposable Docker containers,
including transfers and actual firewall drop counters. Neither script is
collected by ordinary pytest. Their software checks use no paid provider
calls and do not constitute scientific gate or fleet-study evidence.
