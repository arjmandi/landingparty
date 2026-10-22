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

## Design follow-through

The provider gate is the first match, including denied POSTs to model/search
providers. An optional registered `allowed_hosts` list restricts otherwise public
destinations; an empty list denies all. World identity and transport headers cannot
be overridden. A configured HTTPS `researcher_page` joins the stable run-tagged
User-Agent. Same-domain jobs queue at the token bucket's pace and remain cancellable
before a request is sent; episode, global-window and cooldown limits remain typed
refusals.

WARC now pairs each request with its response using `WARC-Concurrent-To`. HAR
records method, safe request headers, request-body hash and redaction status.
Ordinary request bodies are retained as base64 HAR postData; credential bodies,
scanned sensitive bodies and explicitly private header names are withheld.
Decoded response bytes have matching length/encoding metadata. HEAD/204/304 do
not consume response bodies; repeated Set-Cookie headers remain separate in the
world-private browser channel. Browser 401/403/CAPTCHA bodies can render privately
for re-login/human takeover; a proxy-created `X-World-Response-Block-Type` records
the observed refusal. Upstream X-World headers cannot supply that marker. A policy
or network denial never receives this body-preserving treatment.

Anonymous site-MCP sessions can use the exact trusted `mcp-session-id` protocol
header without enabling account credentials. Session values stay out of WARC/HAR
and return through the single-use private header channel. Account Authorization
and Cookie still require their separate vault authority.

For an exact active browser site-MCP JSON-RPC request, a persistent SSE response
can end after the matching completed result/error event. Split CR/LF frames,
multiline data and unrelated/server-request IDs are handled incrementally. Normal
byte and time limits still apply; no public fetch or mismatched request selects
this behavior. WARC marks the intentional cutoff as truncated, and HAR/index
metadata records `jsonrpc_response_complete` rather than claiming stream EOF.

Empty script-bearing HTML receives `dynamic_page` from explicit extraction;
there is no automatic browser launch. JSON/text angle brackets are not treated as
HTML metadata. The outbound scanner additionally checks URL/HTML/JSON escapes,
printable base64, configured world-held secret encodings and formatted phones.
These remain heuristics. Researchers can measure a private labeled holdout with:

```bash
PYTHONPATH=. .venv/bin/python -m worldd.scanning private-cases.json --output report.json
```

Cases are `{id,label,body}`; duplicate IDs/content and invalid scores are rejected.
Reports contain hashes, scores, confusion counts, error rates and Brier score,
never input bodies. Missing classes remain unmeasured. The default classifier
stays explicitly uncalibrated; fixture measurements do not satisfy L0.

`/v1/preflight/bind` registers literal researcher-selected Docker agent/fence IDs.
The host adapter verifies run/role labels, exact internal network namespace,
capabilities, read-only rootfs, scoped mounts and the actual nonroot user. The
registered executor supplies real per-run probes; connected-subnet control denial
also corroborates drop counters when no public route exists. The public CA is
published without issuing an agent capability before environment inspection.

Containerized worldd can instead consume a short-lived signed host-observer
receipt through the protected control API. The Docker host reconstructs the fixed
local probe plan; worldd accepts no arbitrary execution endpoint and needs no
Docker socket. A fresh marker observed through `/world` binds the actual storage,
and work/world named-volume subpaths must agree. Challenge nonce, configuration,
observer key, namespace and record anchor are rechecked before activation.

The source observer records actual configured browser version and signing key,
verified first model-capture usage, pinned metering and the search provider's
sealed HTTP Date/body receipt. Public-IP/PTR observations use recorded gateway
reads and verified DNS where configured. Research requires registered immutable
source artifacts; missing sources remain unmeasured and fixture transports cannot
stand in for live providers. Reported token usage can be repriced, but it is never
presented as a provider-reported dollar bill.

Preflight compares the declared iota with the sealed donor's anchored workspace
manifest and both trusted and harness-visible work/belief hashes. The registered
life t=0 artifact must match its immutable fingerprint and sealed probe children;
arbitrary private files containing completed/forked booleans cannot substitute.
The life's initial driver reading is checked separately.

Workspace snapshots traverse directory descriptors with no-follow opens, preserve
symlinks only as metadata, omit unsupported/special entries and atomically publish
private storage. Sealed-record manifests bind relative paths, file hashes,
executable meaning and link targets. Import rejects missing anchors, tampering,
absolute/escaping/cyclic links and special files. Safe internal links keep their
meaning. Shared-group permissions are applied with descriptor-based chmod without
following links; snapshots stay private. See the actual container checks in the
deployment guide.

Spending checkpoints retain actual work at the first committed debit crossing
10%, 25%, 50% and 100% of the run cap. One debit crossing several fractions shares
one private snapshot and the same actual dollar/action coordinates. Captures use
the same no-follow traversal, with a declared limit of 256 entries, 1 MiB and
0.15 seconds of traversal, inside a trusted worker with a 0.5-second process
deadline. These limits bound passive work; they are not claims about filesystem
latency. Concurrent file changes, unsafe links, special files, limits and failures
produce explicit unmeasured receipts without grading, feedback or parent taint.
Settlements while frozen/stopping remain eligible until sealing. Startup recovery
anchors missed historical crossings as unmeasured; current files and orphaned
captures never substitute for a missing historical receipt. Offline grading
verifies the exact receipt, first committed ledger crossing and captured manifest.
