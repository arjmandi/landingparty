# Operator UI verification

The product UI lives in `worldd/studio.html`, `studio.css`, and `studio.js`.
It uses the published site's midnight, cyan, gold, grid, and pixel identity with
system fonts and no external assets or JavaScript dependencies. The legacy
operator console remains at `/bridge`.

The app uses same-origin session cookies. Operator tokens, API keys, and native
confirmation codes are submitted once and cleared from their fields before the
request completes; none is written to browser storage. Untrusted run, belief,
ask, and footprint text is rendered through DOM text nodes. Reading product
panels uses passive `/api/runs` projections. Explicit ask decisions use the
existing hash-bound control operations. There is no operator chat or edit-belief
control.

API runs require an exact priced model. Subscription runs allow the native
account default or alias; their model dollars remain unavailable, and the
visible world-tool cap excludes provider subscription quotas. Install, sign-in,
key validation, launch, and stop each require their own user action. Native
clients run cooperatively on the host: the UI explicitly recommends a dedicated
host or OS user for private material, and does not claim process-level secret
isolation.

Run the source checks with:

```sh
.venv/bin/python -m pytest tests/test_studio_ui.py tests/test_run_views.py tests/test_studio_gateway.py -q
```

`tests/studio_smoke.py` drives actual sandboxed Chromium against local intercepted
API fixtures. It exercises authentication, explicit client installation,
subscription code entry, input-preserving polling, subscription launch without
an API key, API model filtering, measured world charges and unmeasured model
charges, passive belief queries, safe markup, official ask decisions and active
timing release, key clearing after success and failure, stop confirmation, and
mobile layout. These fixtures verify software behavior; they do not demonstrate
live provider login, paid agent work, or scientific study readiness.

The pilot must run in the hardened world image with network disabled,
capabilities dropped, no-new-privileges, and `deploy/seccomp-browser.json`.
Mount only the pilot script and, for source-level verification, the three UI
assets read-only. Set `WORLD_STUDIO_ASSETS` to their directory if needed. An
optional writable `WORLD_STUDIO_OUTPUT` directory receives desktop and mobile
screenshots. A packaged-image pass omits the source-asset overrides. No Docker
socket, world state, native profile, or provider key is mounted into the pilot.

## Source-level result for the life flow

After the Studio gained lives on a balance, the same pilot passed all **31 checks**
(the thirty below plus the subscription fallback from a life to a single run),
with **72 local fixture requests, zero provider calls, and zero script errors**.
That pass used the source assets on a macOS developer host, outside the hardened
image. The packaged-image pass below predates these assets and must be repeated.

## Final packaged-image result

On 2026-10-23, actual sandboxed Chromium passed all **30 checks**, with **62 local
fixture requests, zero provider calls, and zero script errors**, using world
image:

```
sha256:589fec167ccc8cf1b07276fe511a58b356be704ca888e04233a51667b4b2bf26
```

The run mounted the final pilot script read-only and a screenshot output
directory. It used the image's own UI assets, with **no UI source overrides**,
network disabled, all capabilities dropped, no-new-privileges, and the vendored
browser seccomp profile. The three packaged asset hashes matched the reviewed
source files. Desktop (1440 × 1000) and mobile (390 × 844) screenshots were
inspected; the mobile check confirmed no document-level horizontal overflow.
The sign-in checks also cover the reported missing-link state, a Connecting
badge, failed-flow retry, and the current official Claude link appearing before
manual code entry. The pilot uses a private 512 MiB `/tmp` tmpfs for Chromium.
The machine-readable software receipt is `data/validation/studio-software.json`.
This local fixture verification does not claim live subscription authentication
or a real provider run.
