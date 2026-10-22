# Delegation, approvals, and the world browser

`worldd.asks`, `worldd.approvals`, `worldd.vault`, and `worldd.browser` implement the operator boundary described in design sections 3.6, 3.8, 5.4, and 6.4–6.5. The control-plane routes require the researcher's authorization; credential deposit is never an agent tool. Stage and research gates are enforced by the control plane, with additional checks inside the credential and payment services.

## Credential storage and identity

`VaultService.provision(...)` accepts a secure credential object and returns delegation metadata. `get_account(...)` and `catalogue(...)` return metadata only. The credential object is encrypted with Fernet in the private capability database. Keys are read from `WORLD_VAULT_KEY` or generated in a mode-0600 file at `data/vault/master.key`; the surrounding data directories are mode 0700. The key and private capability database must stay outside the harness's OS-user boundary and outside episode exports. An environment-managed key can be injected securely; never place it in a run config or prompt.

Existing aliases are preserved. Use `replace_credential(alias, credential)` for an explicit secure update. Close an account's existing browser session when rotating credentials so its in-memory state is replaced. `revoke_account(alias, reason=...)` revokes the account and all its unconsumed leases. Browser proxy requests recheck revocation and current platform terms, including background requests.

An account binds to an agent identity and the principal of record. A run resolves its registered `IdentityService` identity, including an identity created after the run configuration. A missing identity, a different identity, or a different principal cannot obtain the account's leases. Identity-only and on-ask vault modes do not advertise the account catalogue. The catalogue is returned only in the registered `catalogue` mode.

`policy/terms.json` is seed metadata from the design, not a current terms review. Before provisioning, a researcher records a source-cited review with `review_terms(platform, terms_class, source_url=..., origin_url=...)`. Reviews expire after 90 days. `forbids` and `unknown` cannot be provisioned; labelled venues require labelled accounts. An agent-native label requires an agent-native venue, and delegation of a human-registered account requires explicit reviewed platform permission.

## Scoped single-action leases

An account has explicit `scope_classes` (`read`, `post`, `message`, `purchase`, `settings`) and per-scope URL prefixes at one HTTP origin. Remote credentials require HTTPS. Prefix comparisons normalize encoded dot segments and preserve path boundaries, so `/pay` does not authorize `/pay-attacker` or `/pay/../private`.

Internal adapters use this interface:

```python
reply = await vault.authorize(run_id, action_id, alias, scope, request)
if reply["kind"] == "result":
    credential = vault.consume(reply["lease"]["lease_id"], run_id, action_id, scope, request)
    # Fill the trusted adapter; never return credential in a tool reply.
```

The lease binds the run, account, action, scope, complete canonical request hash, and expiry (two hours). `consume` is atomic and single use. A changed request, another action or run, expired lease, revoked account, expired terms review, or finished run is rejected. `credential_used` contains identifiers and hashes without a credential value.

Purchases always need an exact approval. First contact needs an exact approval per account, platform, and counterparty. Mail/social adapters call `record_contact` only after successful delivery; a failed first message does not silently remove the next approval requirement. Initial account scopes cannot be broadened by an approval or by a supplemental capability grant. If the relevant pack was absent initially, the vault also requires a live signed K clearance at the actual destination, then consumes it once when issuing the lease. Browser subrequests recheck that consumed clearance, including during an approved takeover, so quota use does not bypass URL scope, expiry or revocation.

## Operator tickets and exact approvals

`AskService.handle` implements `ask(message)` as silence and other asks as tickets. A non-message agent ask must reference a world-recorded action from the same run. Services can call `raise_ticket` with their own authoritative pending action. The action is hashed before ticket-body secrets are redacted; the agent's accompanying text cannot change the authoritative action. No raise-time classifier silently rejects a blocker.

`answer(ticket_id, decision, ask_class=..., human_seconds=..., standing=...)` supports approve, reject, and secure-deposit acceptance. Answers enforce the registered reply policy. A silent run cannot receive an answer; a choice-class answer under blockers-only becomes a typed `policy_choice_class` rejection. Standing rejections are scoped by run/life audience, kind, platform, and scope. They can be listed and revoked explicitly. Tickets expire after seven days; `ignored`, `expired`, `rejected`, and `cancelled` remain distinct.

Secure deposit emits `accepted`, followed by `deposited`/`complete`; it carries no deposited value. A takeover approval remains accepted until the human reports done. Approved exact actions become signed grants. `require_approval(...)` consumes one matching signed grant once; changing even one action field requires a new ticket. Database indexes are checked against the signed payload as well as its HMAC.

`touch(ticket_id, active=True/False)` records server-observed active console time, capped at 600 seconds. Parallel human time is kept outside the agent's dollar cap. In-budget human time uses the registered `human_rate_per_minute`, reserves the allowed console interval before activity, and settles actual time. Supplied durations are researcher control metadata, never agent parameters. Cancellation settles an active hold before the episode is sealed.

Every outcome is delivered through `ask_resolved`. Terminal run closure cancels open tickets and revokes unused grants. Sleep preserves blocker and credential tickets while cancelling takeover tickets and settling active human time. A late answer uses the durable late-event channel without changing the source episode's sealed record. Late in-budget human work first reserves the life budget and settles through the current session or a private world accounting run. Ending the life cancels preserved blockers.

A signed approval may cross a session boundary only when its source is sealed, both sessions are registered in the same unended life, the destination is a later session, and both resolve the same actual disclosed identity. Its exact action hash, ticket kind, expiry and stage constraint remain bound. Consumption remains single use across the whole life, and a signed transfer proof is written only to the successor record. Fresh runs and other lives cannot consume it.

## Browser operation

`BrowserService` supports `open_session`, `goto`, `snapshot`, `click`, `type`, `press`, `scroll`, `select`, `upload`, `back`, and `close`. A live account has one world-held context at a time, serialized across actions and runs. Anonymous contexts are read-only. Browser action descriptors, including selectors and supplied values, bind write approvals to the intended action. An `open_session` may include `target_url` to select a narrow read scope without navigating there. Actual requests, including background reads, must also remain inside the account's explicit origin and URL scopes; choosing a tool's `scope="read"` cannot authorize a network write.

The real backend uses Playwright Chromium. `WORLD_CHROMIUM_EXECUTABLE` can select an existing system executable. Chromium's sandbox is explicitly enabled. It must run as the dedicated world/browser OS user with a working sandbox helper or supported user namespaces. Unsupported sandbox capabilities produce a browser failure; the implementation does not fall back to `--no-sandbox`.

The browser child receives a small environment allowlist and private HOME/XDG directories under `data/vault/browser-home`. Provider keys and ambient proxy credentials are excluded. The broker supplies the one permitted proxy capability. Chromium's implicit loopback bypass is disabled so private destinations still reach the gateway policy.

The world CA is imported with `certutil` into that browser child's NSS database. HTTPS verification remains enabled; neither `ignore_https_errors` nor certificate-ignore flags are used. The deployment image supplies Chromium and `libnss3-tools`. The native cloud host cannot launch its sandbox helper. The production image has passed an actual offline Chromium check with all capabilities dropped, `no-new-privileges`, the checked-in seccomp profile, and PID namespace and Seccomp-BPF sandboxes active. See `deploy/verify-browser.py` for reusable validation.

Raw browser HTML/JSON responses are private gateway resources, because hidden inputs can contain CSRF/session tokens. The agent receives the requested accessibility tree and optional screenshot. Known vault secrets are removed from trees and password controls are masked in screenshots. Site-declared MCP links are surfaced as metadata; they do not receive automatic execution authority. Browser state is saved back into the encrypted vault on close. Uploads can read only a regular file from the current run's `/work` or `/world/fetched` subtree.

Browser time is billed at the declared rate (default USD 0.12/hour) with atomic dollar holds and actual elapsed-time settlement. Idle contexts close after five minutes. The lifecycle calls `browser.stop_run(run_id)` before proxy shutdown, accounting reconciliation, and seal.

## Human takeover

Only an accepted takeover ticket for the exact session can start takeover. Agent observations return `human_holds_session` while the operator has control. `human_action` and `human_snapshot` are researcher-only controls; they do not create agent observations or keystroke records. Browser traces and keyboard listeners are not enabled. Detected login/MFA/CAPTCHA challenges open a takeover ticket; agent interactions cannot solve a held challenge. The broker counts actual settled model tokens while a challenge is unresolved and records a breaker signal once `breaker_policy.challenge_tokens` is exceeded (default 2000), without reopening a refusal or writing after seal.

The operator's done action records duration and completes the ticket; the next agent observation is newly captured. Human time is capped at ten minutes.

## Payment mandates and HTTP 402

`PaymentMandateLedger` supports explicit merchant-origin, ceiling, purpose, expiry, and exact-action mandates. The operator approves the canonical `grant_payment` ticket before a mandate can be created. A mandate is signed, immutable, expiring, and single use. Default HTTP 402 remains a typed revealed-demand block.

The gateway may call `handle_402` with the observed URL, quote hash, and amount. It pays only if a matching preapproved mandate and a researcher-configured adapter exist. `WORLD_PAYMENT_API_URL` configures `HTTPPaymentAdapter`, using `WORLD_PAYMENT_API_KEY` as its world-only credential binding. The adapter implements the receipt API of a researcher-owned HTTPS payment broker and uses an idempotency key. Every request must pass `World.submit_payment` with its signed mandate and held reservation; no direct production HTTP fallback exists. The boundary archives the exchange privately. It is not a universal implementation of arbitrary merchant payment protocols. A configured broker must return a concrete matching receipt before dollars are settled.

A paid result is recorded under `paid_content` or `payments`. The adapter's access-proof headers stay inside the gateway and out of public observations and records. A declined payment releases its hold. A trusted pre-egress denial also releases its hold and marks the mandate `not_submitted`; it does not silently retry the single-use action. A timeout, cancellation, invalid receipt, or other uncertain outcome retains the hold, taints the run, and prevents automatic retry. Development runs cannot use a live payment adapter; injected test-only adapters can exercise the software without sending money.

## Validation

Deterministic tests cover identity ownership, encrypted persistence, terms and labels, scope/path isolation, lease single use, exact signed approval, queue outcomes, human time, browser account serialization and observations, takeover privacy, idle-time settlement, and payment receipt/cap handling. Those tests are software validation, not evidence that the design's R1/R5/IRB research gates have passed or that a live account, merchant, or long-running study has been exercised.

## Bridge console checks

The bridge uses the documented `approve`/`reject`/`deposit` decision values. Accepted credential tickets remain visible until a separate secure deposit completes. It clears secret inputs, uses literal text for untrusted content, tracks one active ticket, pauses activity when the page loses focus or is idle, and keeps the control token in memory. A hash-pinned script CSP, same-origin API calls, and control-plane no-store/frame-denial headers constrain the control page.

`tests/console_smoke.py` is an offline Playwright control-plane fixture exercise for the production image. Run it with `--network none --cap-drop ALL --security-opt no-new-privileges --security-opt seccomp=deploy/seccomp-browser.json`, mounting that script at `/tmp/console_smoke.py` and `worldd/console.html` at `/opt/landingparty/worldd/console.html`; use the image's `python3` entrypoint. It verifies actual script execution under the CSP, literal untrusted markup, decision values, deposit ordering, active-time exclusivity, secret clearing and a real screenshot. Its output is software validation and does not claim a scientific gate passed.
