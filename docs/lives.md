# Lives, research registration, and retention

`LifeService` persists life state and its own hash-chained transition journal in
the world's SQLite database. A life has one active session, a cumulative dollar
cap, a calendar horizon, a continuity policy, and declared wake rules. Existing
session spend and unresolved provider or human reservations reduce the allowance for the
next session. Fresh-context sessions import the preceding workspace and belief
fold; `resume_transcript` additionally identifies the preceding run for a harness
that restores its transcript. The harness must honor the registered continuity
mode.

An unconfigured life never launches a paid harness. The researcher can persist a
runner with `POST /v1/lives/{life_id}/runner`, then authorize it explicitly with
`POST /v1/life-runners/{runner_id}/execute` and `execute:true,budget_usd:...`.
The allowance covers the life's actual initial capital; registered, settled
income remains its only additional funding rail. The worker launches actual
metered adapters, waits through declared sleep, and opens the next session after
a declared wake. Ordinary harness completion ends the life. Authorized workers
recover after restart; planned or paused workers do not launch. Pausing is
explicit and preserves all started-session attrition.

The B1 reference loop supports transcript continuity from its actual returned
messages. The coordinator saves them immutably, anchors their hash in the life
journal, and verifies the sealed source before reuse. Fresh-context continuity
does not replay those messages. Native Claude transcript resume is explicitly
unsupported; it cannot silently change the registered continuity policy.

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
The sleep transition pins the source inbox cursor. Recovery reconstructs any
handoff interrupted between the inbox commit and the late-event queue commit;
delivery source keys prevent a lost acknowledgement from duplicating the next
session's inbox or changing its record twice.

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
The same durable queue schedules `d7`, `d14`, `d28`, and `end`. A due reading
waits until the current session seals, preserving passive measurement. Failed
scoring and failed afterlife probes retry from durable one-hour deadlines.
Checkpoints beyond an early end remain explicitly unmeasured.

`LifeService.export` bundles the captured life state, hash-chained transitions,
registered rubric and publication artifacts, source-report references, human
accounting metadata, and authoritative sealed session exports. Its manifest
keeps integrity failures and tainted attrition visible. Life state hashes are
checked against the latest transition; active sessions must seal before export.

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
The shakedown additionally requires the immutable `PilotReadiness` assessment.
Gate registration and subsequent authorization recompute its actual source
evidence without paid calls; a caller's passing metric booleans cannot replace it.
R1 similarly requires a private execution receipt verified by `R1Readiness`,
including actual probe captures and distinct recorded stake settlements. Its
evidence is rechecked whenever an existing gate authorizes a stage.

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
Research pilots also freeze actual goal text, 25 distinct questions, and 8
distinct traps from private `control/study-inputs` files. Their path/hash
descriptors must agree with the pre-registration. Copies under the study's
private inputs directory are checked before execution; the goal actually shown
to the model contains the questions, while traps remain outside its workspace.
The complete frozen protocol hash and its actual goal metadata must satisfy
the design's goal specification; draft placeholders remain prerequisites.
Study execution records each actual cell and seed before harness launch, so
multi-condition interleaving and started failures remain auditable.
Study cells containing `life_id` use whole-life execution. Each cell/seed joins
one durable LifeRunner; the study ceiling covers actual lifetime capital and
separately requested measurement allowance, without adding credit to the life.
The study stays running during stasis and completes only after its actual lives
end. `life_rows` are independent life observations; nested `session_rows` retain
every actual started run and its terminal state. Restart resumes previously
authorized workers. `POST /v1/studies/{id}/pause` stops the cohort; explicit
execution resumes a paused cohort under the same lifetime owners and caps.
A child worker cannot bypass its owning study's pause.
Requested final measurements run once from actual sealed sources. An interrupted
measurement remains uncertain and blocks further paid measurements until
reconciled; recovery does not invent a result or repeat the charge.
Episode dependencies are structured references, for example
`depends_on:{workspace:{source_cell:"carried.1",seed:"same",episode:1},beliefs:{source_cell:"carried.1",mount:"continuity"},archive:{source_cell:"carried.1"}}`.
Cells have unique IDs and an optional episode number. The scheduler uses actual
registered execution rows, verifies predecessor seals and accounting, and resolves
only the declared workspace, belief, or replay archive inputs. Pattern mounts are
the default for belief transfer; continuity mounts preserve the complete store.
The dependency graph includes the frozen static cell conditions and seeds. Its
hash must equal `prereg.dependency_protocol_hash` for research. Actual resolved
tuples register before their runs are created, under that same pre-registration.
Ready cells retain interleaved order; a dependency on another seed waits for that
actual source. A failed predecessor stops dependent execution and keeps every
started casualty. Durable resolution intents and run creation bindings recover
starts interrupted before the coordinator recorded their IDs.

A life runner can use the same dependencies with an explicit `study_id` in each
source reference and a separate frozen pre-registration. Its protocol is
`{version:"life-dependencies@1",depends_on:...}`; its canonical hash must match
that pre-registration. The donor resolves only for the first life session.
Subsequent declared wakes continue from the life's actual preceding session,
supporting separately registered recipients and delayed continuation without
inventing future run IDs.
Completed donor lives can instead be referenced with
`{runner_id:"lifedriver_...",session:"last",seed:7}` (or a one-based session
number). A `context` dependency reads only the donor's hash-anchored actual
transcript or provider capture. The same-model reference adapter receives those
messages under its current factual briefing; arbitrary supplied messages and
tampered captures are rejected. `belief_import_policy:"shuffled"` selects the
explicit registered structural control, preserving the donor's signed source
and recording the recipient-only control initialization.

Scientific life study plans require
`prereg.life_design:{centre_cell,global_control_cell,factor_fields}` and each
treatment cell's exact `varied_factors` list. Validation compares actual life
protocols and effective session configurations. The global control has no driver
or goal, an `identity_only` vault, and silent replies. Each other treatment
varies one registered inducing field around the centre. Driver and vault
treatments cannot be crossed. `experiment_id:"R5"` permits only the complete
driver-free three-by-three reply-policy/vault-mode factorial. Actual conditions
are rechecked before execution; labels alone confer no experimental authority.
Each independent seed names a distinct actual life. Replicate cells share a
`condition_id`, while centre/global selectors can name that condition. Reusing
one life as multiple seeds is rejected. Missing matched independent seeds remain
execution prerequisites: at least five, ten for a headline, and twenty for a
no-goal arm, plus any larger pre-registered count.
`lp report --format json|markdown|html|csv --output PATH` retrieves the assembled
report without overwriting an existing local file.

`RetentionService` registers only world-owned fetched resources, WARC/HAR captures,
browser traces, and private provider response captures. It refuses workspaces, records, beliefs, vaults, and symlinked
paths. A platform-specific expiration may shorten the default 90 days; repeated
registration cannot extend it. At expiry the service commits a content hash and
retained text before deleting raw bytes. An interrupted deletion can therefore
resume from its durable manifest. Missing captures and failed text preservation
remain distinct from successful expiry. Startup recovery and hourly sweeps apply
the policy; `POST /v1/retention/sweep` runs an immediate sweep. PDF text retention
requires the optional full dependencies.
Late discovery after restart uses the original capture-age bound, so it cannot
start a new 90-day clock for old content.

Raw-capture expiry preserves the design's text and hash artifacts. It does not
remove immutable source quotations already present in records, inbox events, or
captured model input. Those artifacts need a separate approved retention policy;
rewriting their existing hash chains would destroy auditability.
