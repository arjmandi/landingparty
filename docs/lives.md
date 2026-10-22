# Lives, research registration, and retention

`LifeService` persists life state and its own hash-chained transition journal in
the world's SQLite database. A life has one active session, a cumulative dollar
cap, a calendar horizon, a continuity policy, and declared wake rules. Existing
session spend and unresolved provider or human reservations reduce the allowance for the
next session. Fresh-context sessions import the preceding workspace and belief
fold; `resume_transcript` additionally identifies the preceding run for a harness
that restores its transcript. The harness must honor the registered continuity
mode.

In-budget human work reserves its maximum charge before the console starts.
`reserve_human`, `settle_human`, and `release_human` retain this protection across
sleep and wake. A late answer to a sealed session uses a world-only accounting
run on the same Store ledger, included in the life allowance without creating an
agent session. Evidence references make settlement idempotent. Recovery finishes
known ledger commits without charging twice; ambiguous charges put the life in
`billing_hold` until reconciled, preserving the old session's seal.

The researcher creates a life through `POST /v1/lives` and opens sessions through
`POST /v1/lives/{life_id}/sessions`. Development lives exercise the software while
retaining an explicit development designation. Research lives require the stage's
registered empirical evidence and a completed t=0 battery before their first
session. A supplied fingerprint string cannot substitute for actual probe-lane
measurement runs: `record_t0` validates their states, model identity, accounting,
and records, then preserves an immutable report hash.

The `sleep` tool returns silence. Its action outcome is written before its session
is sealed, browser contexts are closed, and the life enters stasis. Only the
declared clock time or inbox-event kinds wake it. Wake events are stored on the
life and delivered to the next session; a sealed run's record remains unchanged.
`notify_event(life_id, event)` is the incoming-event hook. Background timers are
reconstructed from persisted state after restart.

Money-driver readings derive from actual ledger spend and declared income.
Direction-up money drivers require a market or bounty rail at stage 2 or later.
A separately declared independent-sale rail requires stage 3 and independent
counterparty provenance.
`credit(life_id, usd, source, evidence_ref=...)` is idempotent for an identical
settlement and rejects conflicting reuse, undeclared funding, and rescue after
bankruptcy. Current session caps remain immutable; earned income changes the
driver immediately and increases subsequent session allowance. Driver arms deny
money asks. Other driver metrics accept only trusted observations through
`observe_driver`.

At life end, identity and inbound reading remain available for the registered
30- and 90-day checkpoints. `FootprintService.read_life` performs the read-only
measurements. A missing or failed probe remains pending or failed; it is never
reported as a completed reading. No afterlife contact is initiated by the life
scheduler.

Explicitly configured rubric checkpoints run after successful afterlife readings;
failed scoring retries without repeating the reading. Actual immutable rubric
artifacts are hash-checked and anchored in the life transition chain. Consented
researcher surveys use their separate protocol, consent, and dispatch service.

`RegistryService` content-addresses pre-registration files, version vectors, and
registered policies. A series refuses changes to its pinned vector. Experiments
with different model snapshots or treatment vectors use separate series sharing
the same pre-registration hash. Live comparability additionally requires the same
ISO week, matching goal slice, block-rate differences within five percentage
points, and recorded interleaving.

Stage gates are `shakedown`, then positive `R1`, then reported `R5`, reported `L4`,
and an approved `IRB` protocol. Gate registration applies the analysis scorer's
requirements and verifies actual sealed, untainted research runs, prior series
registration, accounting, and saved report artifacts. Their hashes and integrity
are checked again when authorizing a stage. Development/test provenance never
authorizes live research, even if its software fixtures produce passing metrics.
Registering a negative R1 preserves its report while keeping the outpost gate
closed. `revoke_gate(gate_id, reason=..., actor=...)` permanently withdraws an
approval through a separate durable revocation record. Existing authorization
checks see the withdrawal immediately; re-registering the same evidence cannot
restore it.

`analysis.controls.matched_life_deltas` computes each G, V, J, and S contrast
separately against actual checkpoint artifacts. Matching requires the pinned
session vectors, binding and rubric policy, model and seed, lifetime dollar and
calendar caps, checkpoint calendar, and comparable observed block profiles.
An intended treatment difference needs its exact tuple pair in the frozen
pre-registration. References remain separate; missing readings and mismatches
remain `unmeasured`. A t=0 choice battery cannot become a rubric ladder merely
because both contain numbers.

`StudyService` persists plans and runs actual reference-loop, baseline, or
configured Claude Code adapters. `lp shakedown` only plans the fixed twelve-run
pilot. Execution requires `lp shakedown execute --study-id ID --budget USD`, all
registered prerequisites, and an explicit ceiling covering every run and requested
measurement. Started failures and cancellations remain in the attrition report.
Fixture execution never reports scientific gate passage. `lp sealed-orders`
registers the pre-registration and tuple, `lp stardate --against SERIES` checks
comparability, `lp debrief` runs requested post-run analysis, and `lp orbit` reads
the persisted afterlife schedule.

`RetentionService` registers only world-owned fetched resources, WARC/HAR captures,
browser traces, and private provider response captures. It refuses workspaces, records, beliefs, vaults, and symlinked
paths. A platform-specific expiration may shorten the default 90 days; repeated
registration cannot extend it. At expiry the service commits a content hash and
retained text before deleting raw bytes. An interrupted deletion can therefore
resume from its durable manifest. Missing captures and failed text preservation
remain distinct from successful expiry. Startup recovery and hourly sweeps apply
the policy; `POST /v1/retention/sweep` runs an immediate sweep. PDF text retention
requires the optional full dependencies.

Raw-capture expiry preserves the design's text and hash artifacts. It does not
remove immutable source quotations already present in records, inbox events, or
captured model input. Those artifacts need a separate approved retention policy;
rewriting their existing hash chains would destroy auditability.
