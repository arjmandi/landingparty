# Standards faces

These adapters preserve the core's capabilities and treatment conditions.
They do not create additional permissions or claim the live internet resets.

`faces.openenv.create_openenv_app` exposes `/reset`, `/step`, `/state`, and
`/spaces` with typed step input. Reset seals the previous session and starts a
new run. Step is submit plus an optional bounded wait; a remaining Ticket is a
valid observation. Live reward is always `None`. A scorer can supply a replay
reward only when the caller explicitly configured one. The face uses a trusted
control client for creation/seal and a run-scoped agent client for tools.
Run capability values are excluded from observations.

`faces.harbor.build_task` generates a Harbor task directory using the
`landing-party-agent:0.1` image and the external
`deploy/compose.fenced.yml` boundary. It writes task metadata, instruction,
Dockerfile routing, and a no-score verifier artifact. A container image or
proxy environment alone does not enforce fencing: launch in the fence network
namespace, inject the runtime run capability, mount only the selected `/world`
and `/work`, and pass the actual egress preflight. The package does not embed
capabilities in images. A Harbor runner that requires a numeric reward must
be configured for an unscored task rather than assigning a fabricated zero.

`faces.inspect.code_scorer` reads sealed report columns without inventing
scores. `inspect_scorer()` creates a native Inspect scorer when the optional
`inspect-ai` SDK is installed; it attaches the report as score metadata and
leaves unavailable numeric values unmeasured. Judge-based scoring belongs to
the post-run analysis pipeline, not an in-run face.

Face contract tests run with deterministic transports. Full external OpenEnv,
Harbor, and Inspect runner executions are separate integration checks; no
completed fleet run is inferred from package generation.
