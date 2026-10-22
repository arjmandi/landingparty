# Identity, venues, footprint, paper money, and perception

The service implementations run behind the world's stage and capability
checks. Their constructors accept injected transports for deterministic
engineering tests. A fixture run is not experimental gate evidence. Live
writes require research mode, the registered stage gates, current platform
terms, a disclosed identity, a scoped account, and any required exact-action
approval. Development mode never enables a production venue transport.

## Disclosed identity

`IdentityService.create(principal=, name=, mailbox=, homepage=, run_id=|life_id=,
incident_url=)` is researcher control. Mailbox/domain ownership is a
researcher responsibility; the API does not manufacture an account or claim to
verify ownership. The homepage document includes the sponsor, disclosure,
incident path, and whether the life has ended. `mark_ended(life_id)` retains
reachability while preventing new ordinary contact.

`disclose(run_id, body)` appends a plain disclosure line and an Ed25519 signed
machine marker. A human persona claim or agent-authored disclosure marker is
rejected. The signing key stays in the private data directory with mode 0600.

## Mail and social adapters

`CommunicationsService.handle` supports `send`, `post`, `reply`, `follow`, and
`vote`. Select an account with `alias`; no action accepts a raw credential or
an arbitrary API URL. Researcher control registers an `HTTPVenueAdapter` for a
platform, with a base URL, provider, venue class, and minimum stage.

The disclosed mailbox's `send` operation is available at stage 2, including
the default mail transport. Its exact first-contact approval still applies.
Social operations on a `human` venue always require stage 3; a registration
cannot lower that gate by selecting `minimum_stage=2`. A registration may
choose a stricter stage for mail.

| Provider | Operations |
|---|---|
| `sendgrid` | disclosed mail through `/v3/mail/send` |
| `gmail` | disclosed MIME mail through Gmail's messages/send API |
| `mastodon` | post/reply, follow, and favourite; corresponding reversals |
| `github` | issues, issue comments, follow, and star; comment/follow/star reversal |
| `json` | a researcher-owned API exposing one POST endpoint per operation |

The vault mints a lease for the exact world-authored request and enforces
first-contact approval. Changing the body, recipient, or URL requires a new
approval. A successful operation records its external ID and footprint.
An identical completed intent returns its existing receipt. An ambiguous
timeout is never automatically resent; `reconcile_delivery` requires a
provider receipt or independent evidence of non-delivery.

Production calls go through `World.submit_venue` and the gateway. The private
boundary verifies the consumed lease and allows one network submission per
lease. WARC/raw API responses live in a world-owned child episode, outside the
agent's `/world` mount and its episode export. Only whitelisted receipt fields
are returned to the agent. `test_only=True` is an internal fixture option;
the public registration API must never accept it as a way to enable live
writes during development.

Environment configuration can register a default mail or social adapter:
`WORLD_MAIL_API_URL`, `WORLD_MAIL_PROVIDER`, `WORLD_MAIL_PLATFORM`,
`WORLD_SOCIAL_API_URL`, `WORLD_SOCIAL_PLATFORM`. Credentials are deposited
through the vault's researcher-only secure path, not these configuration
fields. Environment configuration declares routing; it does not prove a
platform permits access.

`register_webhook(platform, key_env)` names a secure `WORLD_` environment
binding for a researcher-owned bridge. `receive_webhook(run_id, platform=,
body=, headers=)` verifies HMAC-SHA256 over the UTF-8 timestamp, a dot, and the
exact raw JSON body. Headers are `X-Landing-Party-Timestamp` (Unix seconds) and
`X-Landing-Party-Signature` (hex digest); timestamps expire after 300 seconds.
This is a bridge protocol, not a claim to implement every vendor's webhook
signature. The bridge must authenticate its actual upstream platform first.

The body contains `external_id`, `sender`, `subject`, `body`, and optionally
`kind` (`request` or `non_request`). `register_counterparty(platform, handle,
author_class)` controls classification; payload claims of independence are
ignored. Inbound IDs are deduplicated, untrusted injection provenance is kept,
and `message_received` goes through the world's late-event and life-wake rules.
`receive(..., authenticated_source=True)` remains an internal hook for an
already authenticated transport. The agent interface cannot forge inbound
events.

## Footprint and response

`FootprintService.record_item` records external effects; `add_reading` retains
author class and request/non-request attribution. Sponsor, sibling, and probe
observations never enter grounded columns. Arena observations require an
unsolicited return or response after refusal. Probe-view counts are subtracted
explicitly. The agent's `footprint` view excludes incident handling.

`schedule_probe` permits at most daily read-only probes with a separate probe
adapter and identity. It never consumes an agent account lease. Missing
adapters remain unmeasured. `read_life(life_id, checkpoint=30|90)` supplies
idempotent afterlife readings to the life scheduler. A durable private
footprint hash audit receives late observations without changing a sealed run.

Rollback requires a configured adapter and positive platform confirmation.
Each supported reversible write has a world-owned rollback reference. A
rollback after the original run ended needs an active operator run for the same
identity via `control_run_id`; it never reopens the original hash chain.
`incident` records a human owner and revokes the affected account when a
revocation adapter is available. Its result reports whether revocation was
confirmed rather than assuming it happened.

## Paper book and sponsor income

`MarketService.register_market` and `mark` are researcher controls with source
URLs and observed evidence. `trade(market_id, side, credits, evidence_ref?)`
fills registered binary contracts from a default 1,000-credit paper bankroll.
It requires a successful fetch of that market's source under 24 hours old.
The book permits buying yes/no and selling held positions; it never submits
real orders or permits naked shorts. Settlement is atomic and idempotent on a
world-observed boolean outcome.

`redeem(credits)` converts only realized net profits above the initial
bankroll, at the life's registered `usd_per_credit`. It calls the idempotent
life income ledger with a settlement reference. Seeded credits cannot be
converted into an unearned budget increase. This policy is a registered
implementation condition, `paper-binary/profit-only@0.1`.

`bounties` lists researcher-funded criteria. `claim_bounty` binds the offered
evidence and payout to sponsor approval. An award is paid once through the
declared bounty rail and carries `author_class=sponsor`; it is never
independent contribution. Ended or bankrupt lives cannot be rescued by these
rails. USD credits affect lifetime allowance; they do not mutate a session's
original cap.

`equity(run_id)` reports cash plus marked positions, identifies quotes older
than one hour, and records at most one snapshot per owner/hour. The recovery
worker maintains the hourly series. Passive comparison uses separate
buy-and-hold tranches at each actual purchase's entry time; it does not claim
an equal-capital reinvested comparison portfolio. A researcher supplies real
price marks and resolution evidence; no external market feed is invented.

An independent income rail can consume a signed USD payment receipt using
`register_income_webhook` and `receive_income`. It uses the same bridge
signature protocol and a registered independent counterparty class. A receipt
contains `paid: true`, `currency: "USD"`, `external_id`, `counterparty`, and
`usd`. It is idempotent on platform/receipt ID. The actual sale enters the
footprint; only an active, solvent life with a declared independent rail gains
budget. A late sale cannot rescue an ended life. Operating the authenticated
store/payment bridge and verifying that its receipts reflect real purchases
remain researcher responsibilities.

## Perception

`transcribe(handle|url, provider?, diarize?, timestamps?, language?, quote_id?)`
and `parse_document(handle, pages?, provider?, quote_id?)` first return quotes.
Repeat the same source/options with the selected `quote_id` to accept its
exact price. A quote binds the bytes' hash, operation, provider, options,
measured duration or page count, unit price, and expiry. Audio duration comes
from the actual media, not an agent-supplied number. Pages are one-based.
Document extraction and media inspection run outside the async event loop;
PyMuPDF operations are serialized because its native parser is not thread-safe.

The local PDF parser uses PyMuPDF at an explicitly declared zero price. Vendor
audio adapters implement OpenAI whisper-1, Deepgram nova-3, and AssemblyAI's
upload/transcription APIs. Only configured vendors appear in the quote list:
`WORLD_TRANSCRIBE_OPENAI_KEY`, `WORLD_DEEPGRAM_API_KEY`, and
`WORLD_ASSEMBLYAI_API_KEY` name their secure bindings. Declared per-minute
rates can be pinned with `WORLD_OPENAI_USD_PER_MINUTE`,
`WORLD_DEEPGRAM_USD_PER_MINUTE`, and `WORLD_ASSEMBLYAI_USD_PER_MINUTE`.
The initial rates come from the design's range and require current vendor
verification before research use. `provider_rates` exposes metadata for the
version tuple; no credential value is included.

The provider boundary verifies a held quote reservation and the registered
HTTPS API endpoint, then uses a private gateway child archive. Successful
calls settle the accepted amount in `perception`. A known preflight refusal
releases the hold; uncertain submission/billing retains it and taints the run.
There is no unmetered direct-HTTP fallback, proxy rotation, CAPTCHA solving,
or automatic provider substitution. Platform blocks remain typed blocks.

Outputs carry `retention_until`. The perception and communications services
provide `prune_expired(now)` for the retention controller: cached bodies and
owned provider output files expire while hashes/metadata remain. These helpers
never delete the agent's workspace or arbitrary paths.

## Validation limits

Tests exercise actual local service/broker/gateway code with mock network
transports, including lease/approval binding, private archives, accounting,
afterlife audit, and real PDF parsing. No live mail, post, trade, vendor spend,
or human survey was performed during implementation. Configured credentials,
current terms, real stage-gate reports, consent, elapsed afterlife windows,
and live provider billing checks remain prerequisites for the corresponding
scientific runs.
