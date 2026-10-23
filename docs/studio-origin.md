# Studio origin and operator access

`worldd` serves the browser studio and control API at one origin, by default
`http://127.0.0.1:7777`. There is one trusted operator per instance and no user
account system. Binding beyond loopback requires `WORLD_CONTROL_TOKEN` in the
server environment. Set that token before starting a cloud or container instance.

The browser loads `/` and `/assets/*` before authentication. It checks
`GET /api/session`, which returns `{authenticated, requires_token}`. When a token
is required, submit it once as JSON `{token}` to `POST /api/session` from the
same origin. The response establishes an opaque eight-hour HttpOnly,
SameSite=Strict cookie, with Secure enabled on HTTPS. The operator token is never
returned or placed in browser storage. `DELETE /api/session` revokes the cookie;
server restart, expiry, or token rotation also invalidates browser sessions.

Cookie-authenticated mutations require an exact same-origin Origin or Referer.
Script clients can continue using `X-World-Control-Token` or a control bearer
token without a browser session. Local instances without a configured token
remain trusted local control interfaces; cross-origin browser mutations are
rejected. Without a token, the control application accepts only literal loopback
Host names (`127.0.0.1`, `localhost`, `::1`), blocking DNS rebinding even if a
forged Origin matches the attacker's Host. Forwarded Host headers do not grant
local access. The legacy `/bridge` page and `/healthz` are protected in studio mode.

The shared origin preserves separate capability boundaries:

- `/agent/v1/*` exposes only run-scoped action, status, manifest and inbox APIs.
- `/model/*` exposes the existing model lane and requires a valid run capability.
  For example, use `/model/v1/chat/completions` with `X-World-Run-Id` and the run
  bearer token. The same durable metering and provider policy apply.
- `--public` enables the optional `/public/*` identity and incident boundary.
- The remaining routes belong to the operator control application.

The agent, model and public applications are mounted outside operator-auth
middleware. A run token cannot authorize control, and an operator browser cookie
cannot authorize a run or model call. The source control application's lifespan
starts and closes exactly once.

The recorded proxy remains an internal TCP service on loopback port 8080; it is
not a browser front end. `--no-proxy` disables it. Trusted container deployments
can set `--proxy-host 0.0.0.0` to reach the proxy through the private container
bridge; keep that TCP port private rather than publishing it as a front end.
`--legacy-listeners` restores
the original control 7777, agent 7778 and model 8081 layout. Explicit old
`--agent-port`, `--lane-port`, `--public-port` or `--proxy-port` options also retain
that legacy layout for compatibility; zero disables an optional listener. Legacy
API clients and the bridge keep their existing authentication behavior.
