# Fenced deployment

The world has internet access; the agent shares an internal default-drop network
namespace with a firewall sidecar. Only agent tools (7778), recorded proxy (8080)
and model lane (8081) at 172.30.77.2 are allowed. Researcher control (7777), direct
public IPs, direct DNS and IPv6 egress are denied. Only the sidecar has NET_ADMIN.
World/agent drop all capabilities and retain no-new-privileges. Per-run mounts
expose writable work and read-only world data, never world-private storage.

## Build

From the repository root with Docker 28+, BuildKit and Compose v2:

```bash
docker build --target world --tag landing-party-world:0.1 .
docker build --target agent --tag landing-party-agent:0.1 .
```

Images use Python 3.12.14, uv 0.12.23 and `uv sync --frozen --extra full --no-dev`.
Debian Chromium, sandbox helper and NSS tools come from signed apt repositories
over HTTPS. Downloads retain TLS verification. Source files are readable by the
unprivileged users; newly created private runtime storage stays private. The
world image includes policy, goals and preregistration templates. The agent image
keeps only client modules, runtime adapters, dependencies and firewall init;
research banks, grading code, server internals, design and study material are
absent. Assigned instructions arrive through the run preamble, not a goal catalog.
Rebuild after source or lock changes.

This managed cloud needs its public platform trust bundle and existing outbound
HTTP(S)_PROXY values. The verified build uses a BuildKit secret for the public CA
(no private key):

```bash
export DOCKER_CONFIG=/workspace/.cache/docker
docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY \
  --add-host proxy:172.31.14.125 \
  --secret id=environment_ca,src=/etc/ssl/certs/ca-certificates.crt \
  --target world --tag landing-party-world:0.1 .
docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY \
  --add-host proxy:172.31.14.125 \
  --secret id=environment_ca,src=/etc/ssl/certs/ca-certificates.crt \
  --target agent --tag landing-party-agent:0.1 .
```

The proxy address is specific to this environment; inspect current platform
configuration before reuse elsewhere. System trust includes the public CA without
disabling TLS or apt verification. `.dockerignore` excludes private data, keys,
credentials and environment files.

## Verify actual containers

After building both images, from the repository root:

```bash
python deploy/verify-fence.py
python deploy/verify-browser.py
python deploy/verify-world.py
```

The firewall check creates/cleans temporary containers and a network. It verifies
an owned reachable tool fixture; blocked control/public curl/Python/DNS;
unchangeable default-drop rules; increasing drop counters; and no agent NET_ADMIN.
No successful internet observation is implied by the fixture.

The browser check runs the production world image with network=none, ALL
capabilities dropped and the supplied seccomp profile. It renders an owned page,
captures tree/screenshot and reads `chrome://sandbox` for actual PID namespaces
and Seccomp-BPF enforcement. Reports go to
`data/validation/{fenced,browser}-software.json`; nonzero exit means failure.
Both checks passed here on 2026-10-22. They explicitly report software evidence,
not scientific research gate evidence.

The world check verifies actual image startup, protected control, agent scope and
sealing in a temporary container. Its optional `--live` flag attempts free public
example.com tool/proxy reads and saves a separate `world-live-software.json`.
Local checks passed here; live reads currently fail closed because this cloud's
upstream proxy refuses pinned-IP CONNECT (ProxyError before TLS). DoH resolved
the public target successfully. The tool returned `robots_unavailable`; direct
hostname fallback was not attempted. This external constraint is preserved in
the generated evidence, not counted as a passing live check.

`seccomp-browser.json` preserves modern Moby's default-deny profile and clone3
ENOSYS compatibility rule, adding Chromium's unprivileged namespace calls
(`clone`, `setns`, `unshare`) and `chroot`. Kernel capability checks still apply:
Chromium calls chroot after entering its own user namespace. Namespace/seccomp
sandboxes stay enabled. No `--no-sandbox`, ignored TLS errors, privileged world
container or host setuid change is used. A kernel allowing unprivileged user
namespaces is required. Chromium has private writable HOME and imports the exact
world CA into its NSS database.

Upstream profile sources fetched with verified HTTPS:

| Source | Download SHA-256 before local edits |
| --- | --- |
| [Moby default](https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json) | `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6` |
| [Playwright v1.64 namespace guidance](https://raw.githubusercontent.com/microsoft/playwright/v1.64.0/utils/docker/seccomp_profile.json) | `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849` |

The vendored file is authoritative; verification records its hash and image ID.
Review upstream updates before replacing it.

## Launch a run

Provide `WORLD_CONTROL_TOKEN` through trusted environment/secret settings, never
in run JSON, binding files, images or agent workspaces. Run Compose from `deploy/`
so the seccomp path resolves:

```bash
cd deploy
docker compose -f compose.fenced.yml up -d world
```

Control is published on host loopback 7777 only. Create a run through the trusted
control interface (`lp energize` or POST `/v1/run`) with
`isolation_profile: fenced`. Its directory must exist before agent startup because
named-volume subpaths select that exact run. Development mode refuses production
external writes. Research mode begins in preflight, withholds agent capability,
and starts the episode clock only after activation.

This cloud also needs `WORLD_PLATFORM_PROXY_IP=172.31.14.125` and its existing
HTTP(S)_PROXY environment when launching Compose. Those upstream proxy settings
go only to the trusted world service; they are separate from the agent's scoped
world proxy. Other environments may use direct world egress or their own reviewed
outbound proxy. The optional `proxy` host alias defaults to loopback when unused.

Trusted deployment code must call `PreflightService.run` with actual harness
environment, executor inside the agent namespace, firewall inspector, private
world-observed metadata and evaluation/t=0 references. The HTTP API does not accept
executable callbacks. Missing egress/rDNS, provider billing, version or fresh live
fixed-bank observations remain unmeasured. Fixture scripts cannot replace these
per-run checks.

After creation/activation set process environment `WORLD_RUN_ID` and
`WORLD_AGENT_TOKEN` from its result. Construct `WORLD_PROXY_URL` using the run's
proxy username/password and reachable `http://172.30.77.2:8080` address. The
listener bind address `0.0.0.0` is not a remote destination. Keep proxy capability
only in launch environment. Raw shell networking needs run option
`raw_lane_enabled: true`; otherwise the proxy returns typed raw-lane denial.
Start the agent after setting these values:

```bash
docker compose -f compose.fenced.yml up -d fence agent
```

Agent UID/GID is 10002:10002, with read-only root storage, writable private HOME,
`/tmp` and `/work`, and read-only `/world`. World uses shared group 10002 only for
work permissions (0770); its private directories remain inaccessible. Protect
runtime proxy/lane variables against binding-file overrides. The public CA is
`/world/ca.pem`; provider/control keys remain world-held.

`docker compose -f compose.fenced.yml down` stops services and retains run data.
Do not remove volumes unless deliberate data deletion is intended. Never give
the agent an extra public network, NET_ADMIN, Docker socket or private world data.

## External observations

`WORLD_DOH_URL` configures verified HTTPS DNS (compose defaults to dns.google).
Public destination checks still apply. `WORLD_IDENTITY_URL` enables real Ed25519
HTTP signatures. Publish the public key directory at that exact HTTPS URL, for
example reverse-proxy `/.well-known/http-message-signatures-directory`; no signing
key means HTTP 404. Static egress/rDNS, third-party signature acceptance, reviewed
terms/prices and a fresh actual 300-URL block profile need operator observations.
No paid pilot or long-horizon study is claimed by this deployment.
