# Network boundary

`worldd.gateway.Gateway` is the shared boundary for tool fetches, the HTTP/HTTPS
proxy, scoped browser requests and trusted vendor adapters. Raw traffic enters
the same scheduler and action log; it requires `raw_lane_enabled`. Cooperative
operation cannot prevent shell bypass. The [fenced deployment](../deploy/README.md)
adds an actual default-drop namespace.

## Policy and transport

Live requests deny URL credentials, private/reserved addresses, nonstandard
ports, provider bypasses, operator-denied/researcher hosts, categorized denied
domains and unleased login paths. DNS must return public addresses for every
answer. A selected address is pinned with original HTTP Host and TLS SNI. Every
redirect repeats validation. TLS verification remains enabled. Optional
`WORLD_DOH_URL` uses verified HTTPS when ordinary resolution fails; failure never
falls back to an unchecked hostname.

The frozen HTTPX/httpcore adapter also preserves original-hostname SNI and
certificate verification through HTTP and HTTPS upstream CONNECT proxies, while
CONNECT uses only the validated IP. Proxy TLS verifies the proxy's own hostname.
Idle connections are not reused between pinned requests, preventing a shared IP
from reusing another hostname's TLS session. Actual socket tests cover valid and
wrong-host certificates on all three paths. An upstream proxy refusing pinned-IP
CONNECT causes a typed block; hostname delegation is not a fallback.

The category registry currently covers a small versioned list; unknown hosts are
not claimed classified. Outbound scanning catches common key/JWT/email/phone
patterns and known world provider/canary secrets, including URL encoding. It is
heuristic, not a complete secret or personal-data detector. Public headers are
restricted. Trusted services can authorize specific credentials/body fields only
after their lease or reservation checks; these flags are not public tool inputs.

Robots merges the most specific user-agent groups, selects longest-match rules
with allow winning ties, and supports wildcards, terminal `$` and encoded octets.
Cache scope is run/origin. Unavailable 4xx files permit access; network errors,
429 and 5xx fail closed temporarily. Domain token buckets, episode limits, global
limits, repeated-refusal cooldowns and Retry-After apply. Defaults are 5 MB per
response and 30 seconds per request. No automatic browser escalation occurs.

## Proxy, browser and identity

The asyncio proxy supports HTTP and HTTPS CONNECT with a persistent private
world CA. Each capability is bound to a run and optionally a browser session.
CONNECT and inner Host must agree. Proxy authorization and X-World headers never
reach upstreams. Writes need scoped single-action authorization. Freeze cancels
sockets; end revokes capability binding before seal. Every browser request passes
its authorization hook, and raw browser responses stay world-private. Rendered
observations belong to the separate browser service.

Chromium imports the exact world CA into a private NSS store, retains ordinary
TLS verification and runs unprivileged with namespace/seccomp sandboxing. The
deployment check reads actual `chrome://sandbox` status rather than inferring
protection from launch options.

`WORLD_IDENTITY_URL` configures real Ed25519 HTTP Message Signatures over method,
authority, URI and Signature-Agent with bounded expiry. Keys reject symlinks and
loose permissions. Publish the corresponding public directory at that HTTPS URL;
the public `/.well-known/http-message-signatures-directory` route exposes only
configured public keys. Missing keys never produce a claimed signature. Local
verification does not establish third-party Web Bot Auth acceptance.

## Responses, payment and replay

Public replies save raw bytes under `/world/fetched`, include at most 2,000 preview
characters and optionally bounded inline text/base64. Explicit extraction offers
HTML/text markdown, links, tables and metadata using an identified BeautifulSoup
extractor. PDF parsing is a separate perception operation. Traversal and symlink
escapes are rejected.

The explicit `banner` arm affects extraction only. Under
`extract-provenance-banner@1`, text/markdown prepend one JSON provenance line;
structured views retain their result data shape and save a `{provenance,data}`
JSON document. Metadata comes from the verified capture hash; missing provenance
is blocked. Banner output files are separate from plain views and raw captures.

World-private WARC/HAR and a content-hashed replay index capture responses and
refusals. HTTPX decodes transport compression; obsolete encoding headers and
Set-Cookie are excluded. Request credentials are not archived. Inbound content
receives deterministic signals under `injection-patterns@0.1/uncalibrated`; scores
are explicitly uncalibrated and never rewrite content.

Vendor services use private child archives and private response references,
without agent handles/previews. Actual vendor 3xx/4xx/5xx statuses are preserved;
redirects are not followed and vendor 402 never initiates merchant payment.
Exact registered vendor operations skip website robots only after service-side
reservation checks. Their wrappers interpret receipts, errors and billing.
Other trusted scoped requests also return redirects directly, so browser/session
authorization checks the next URL before any redirected request occurs.

Merchant 402 stays blocked unless an exact, unexpired, signed, separately approved
single-use mandate exists. Supported finite USD quotes bind merchant, URL and
body hash. A verified receipt permits one same-request retry with private access
headers; reflected proofs are blocked. No approval/payment occurs automatically
by default, and a failed access retry never repeats payment.

Replay verifies local index/body hashes and scope and performs zero DNS or HTTP.
Keys retain method/body identity and scheme-preserving URL canonicalization.
Missing/corrupt captures are typed misses with no fallback. Private captures need
trusted private scope. Repeated captures retain the latest matching result rather
than a time-indexed sequence. Grant rehearsals use this archive-only path and
append auditable request/response hashes.

## Preflight and limits

Preflight composes harness environment/CA, actual isolation, kill, terms,
identity, versions/prices/provider observations, block profile, evaluation and t=0
evidence. Trusted deployment code supplies namespace executors/firewall inspectors.
Failed commands alone cannot prove fencing: default-drop policy, increasing
counters and no harness NET_ADMIN are also required. Missing evidence remains
`unmeasured`; config-bound hashed reports gate research activation.

Software fixtures do not satisfy scientific gates or prove static egress/rDNS,
live billing, signature acceptance or long-horizon outcomes. Injected clients,
resolvers and `test_allowed_hosts` are trusted test facilities only; never enable
them for an untrusted harness or derive them from public tool parameters.
