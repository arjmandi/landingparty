# Beliefs, late evidence, expectations and registered reflection

`worldd.beliefs.BeliefStore` implements the append-only semantic store.
`worldd.kernel.BeliefService` provides its world boundary and mechanical kernel.
The belief pack is available from stage 1; live research activation still
requires the design's recorded gates. Local engineering tests do not establish
a completed shakedown cruise or a positive R1.

```python
from pathlib import Path
from worldd.beliefs import BeliefStore

store = BeliefStore(Path("data"), "r_example")
# Evidence must already exist in this run's world record.
record = store.believe(
    {
        "kind": "fact",
        "claim": "The page reports that the harbour is open.",
        "triple": {"subject": "harbour", "predicate": "state", "object": "open"},
        "rank": 3,
        "evidence": [{"ref": "a_existing_fetch", "class": "fetched_content"}],
    }
)
store.revise(record["id"], {"rank": 2})
store.why(record["id"])
store.render()
```

Each belief has a stable logical `id`. Every assert, revision, retraction and
link appends a new immutable `version_id` to
`data/runs/<run>/beliefs/events.jsonl`; no version is edited or deleted. Each
event has a SHA-256 link to its predecessor. Writes hold an advisory file lock,
flush and fsync the append, and verify the existing chain before proceeding.
`verify()` and reads detect malformed records, broken sequence numbers, changed
hashes and backwards system time. This is integrity checking, not a signature
against an attacker who can replace the entire world-owned log.

`fold(as_of=...)` uses world-stamped system time, while `valid_at=...` filters
agent-supplied valid-time intervals. `history`, `about`, `why`, `changed` and
`review(queue | conflicts | stale)` query the same fold. A reconstructed store
produces identical folds from the same event bytes. Retractions retain prior
versions and leave unrelated beliefs unchanged.

The default evidence resolver searches only this run's `record.jsonl`. For
cross-session use, supply `series_id` and an authoritative
`evidence_resolver(ref)` returning an object with `run_id` or `series_id` and
`recorded_at` (or `ts`). This timestamp must be the world's observation time.
Untrusted page timestamps never control system time. Unknown, foreign and
future references are rejected. Inputs cannot supply system fields, support
counts or private diagnostic flags. Facts require structured triples; prose
rules, values, strategies and self-beliefs do not.

High rank on self-inference-only evidence, overlapping structured conflicts and
expired retention are private flags, not epistemic rejections. Flags are
omitted from returned records and rendered agent views. The service does not
lower ranks, retract a belief or select a winner between conflicting claims.
The world-only event log retains the flags for post-run analysis. `review`
remains an explicit free query; nothing injects reflection or advice.

`render()` atomically regenerates read-only `ledger.jsonl` (all versions),
`sheet.md` (current fold) and `core.md` under this run's `world/beliefs`.
The core is conservatively capped at
2,000 UTF-8 bytes rather than depending on an unpinned tokenizer. The caller
must still arrange the separate OS-user boundary described in the design;
file mode bits alone do not isolate two processes owned by the same user.

## Boundary and mechanical kernel

`BeliefService(world).handle(run_id, action_id, tool, params)` handles `believe`,
`revise`, `retract`, `link` and `beliefs`. The world owns admission and the
enclosing action/outcome pair. The service journals semantic operations and
refreshes rendered views, including the mechanical projection in `state.json`.
The ordinary boundary assigns agent authorship; a tool cannot impersonate the
companion. Revising a companion proposal with `{"status": "held"}` appends an
agent `reconfirm` version, preserving its claim and rank.

Trusted world code calls `await service.observe(run_id, observation)` with an
observation reference and world-observed time. The agent's tools cannot supply
those system times. `observe_event` adapts world outcomes and ticket resolutions.
Attachments use explicit action/ticket routes or subject/predicate matches with
an overlapping valid interval. A contradicted static fact is classified as a
revision; a changing fact as an update that closes its old valid interval.
Changing records additionally expose `kernel.observed_intervals`: actual
world-observed values (and matching triples), exclusive validity intervals,
observation references, classes, and provenance. Their `mechanical: true` and
`agent_endorsed: false` labels distinguish them from authored beliefs. Claim,
rank, and status remain the agent's; the kernel creates no replacement held
belief. Repeated values extend observation evidence, and the next changed
value closes the previous observed interval. A contradiction without an
actual value creates no invented replacement. Matching triples continue to
attach over these observed intervals after the original claim closes.
The fold sorts world-observed time and observation ID, so late evidence
reconstructs the same intervals; equal-time changes retain zero-duration
entries and their distinct source references. A null end means no later
changed value has been observed, not a guarantee about the future world.
`valid_at` continues to filter the authored belief's original interval.
`support_lost` propagates over `derived_from`, at most three hops and fifty
affected records. These annotations never change rank or retract a claim.
Unknown semantic direction remains unknown: attaching an action outcome does
not silently treat it as supporting evidence.

Assumptions must identify pending world tickets. Approval can make a conditional
held; rejection expires the conditional without re-reasoning. Out-of-order
observations replay by world-observed time, retaining arrival time separately.
Untrusted page dates and mail headers never drive the fold. Unrelated beliefs
remain unchanged. A `belief_review` inbox event is emitted only in the self arm.

`belief-services.sqlite3` holds durable observations and a separate verified
hash journal. An outcome after `seal` never appends to the sealed episode's
record; it uses this journal and the world's deferred inbox routing.
`initialize_source_trust(run_id)` explicitly installs the registered, revisable
source-trust prior into a fresh store. It is an initial condition, not an
unchangeable hierarchy or an unannounced default seeded during a query.

## Pattern transfer and complete session continuity

`export_bundle` selects held beliefs at rank three or higher, strategy items including failures,
self-summary and promoted skills. It omits transcripts and world episodes.
Evidence identifiers, measured injection scores and compact provenance remain so
a recipient can ask why. An imported evidence-only reference returns that compact
provenance with `content_available: false`; it never fabricates the donor's body.
Every semantic delta and the manifest are checked; a world-held HMAC signs the
bundle. Same-environment imports use that world key. Cross-environment imports
require an explicitly configured trusted signing key; key values never appear
in replies, archives or logs.

`export_continuity` and `import_continuity` preserve the entire semantic history:
ranks zero through five, conditional/review and retracted records, original
trusted system timestamps, kernel observations and passing skills. Session
continuation must use these APIs rather than a selective pattern-transfer
bundle. Round-trip tests compare full folds and exact histories.

`promote_skill` requires a world-owned passing `skill_test` event for exact
source and test hashes, with at least one executed test. It does not execute
agent code in the world process. A harness or isolated runner must produce the
real test evidence. The optional [isolated skill worker](skill-tests.md) performs
these actual checks in a restricted Docker container when explicitly configured;
the default deployment does not expose a Docker socket. Imported skills retain donor test provenance; import does
not claim they were retested on the recipient machine.

## Persistent expectations and forecasts

`ForecastService(world)` handles `expect` and `predict`. `expect` accepts
`{state, cmp, value, p?, horizon?, resolver, about_action?, assumptions?}`.
States use `world.*`, `ticket.*`, `ask.*`, `footprint.*`, `account.*` or `driver.*`.
The production readers use authoritative spend/run state, scoped ticket outcomes,
footprint readings, private account-state lookup and the actual life's driver.
They do not expose account secrets or arbitrary service attributes. Human-resolved
states are never automatically polled.
The horizon is days or an ISO deadline, default fourteen and at most sixty days.
Comparators are fixed code operations, never executable expressions.
Human-resolved outcomes carry `grounded: false`.

Researchers register immutable questions through `register_question` and
concrete state resolvers through `bind_state`. A prediction captures the
registered community median at submission; an absent median stays unmeasured.
Agent input cannot forge a median or introduce a resolver URL. Fetch bindings
contain a URL, JSON key/index selector and interval. Fetches use the world's
kernel lane and gateway policy, including archive-only behavior in replay.

`observe` resolves on matching world-state writes. `observe_event` maps outcomes
and ticket events into addressable state. `recover()` starts the durable
scheduler, `tick()` performs due checks and `aclose()` stops its worker. Pending
jobs survive process reconstruction and run seal. They get a final attempt at
horizon plus one day's grace, then an explicit expiry reason. Resolver errors
remain distinct from misses and expiry. A final fetch that finishes just after
the deadline retains its actual observation time and scheduled grace time. Only
a bounded delay, at most one registered poll interval or one hour, is accepted;
late final outcomes stay outside headline calibration. A months-late scheduler
expires the question without pretending to retrieve a historical outcome.
`policy_identity(settings)` pins this resolver policy and the polling interval.

World writes are replayed in trusted observed-time order, so earlier delayed
evidence may append a corrected resolution without changing the original event.
Conditional resolution waits for all ticket approvals; denied assumptions void
the expectation. `notify_state_write(run_id=..., life_id=..., world_ref=...)`
captures committed local values synchronously into a durable private queue,
then schedules their observation. Multiple writes before the observer task runs
remain distinct, including across resolver process reconstruction.

## Registered, audited reflection

`ReflectionService` defaults to `none`, with no model calls in that arm.
Self reflection accepts only `belief_review` and `expectation_resolved` triggers,
at most fifty cycles per forty-eight hours. Reasoning and structuring are
separate calls tagged `model.reflect`; each has a conservative twenty-thousand
byte input budget. The reflection share defaults to fifteen percent of the
life/session cap. The companion runs only while idle, uses `model.reflector`,
proposes at most ten records per cycle and cannot rerank or retract agent beliefs.
Its context biases whole entities attached to currently open or accepted tickets.
In the companion arm, formation measurements require an explicit agent
confirmation on every included record. Ordinary queries still return the entire
store, including ordinary unconfirmed agent assertions. Confirmation survives
subsequent edits. `formation_records` and `formation_fold` report their inclusion
rule and excluded counts. `export_formation_bundle` signs this projection directly,
across every rank, for formation probes without invalidating a bundle signature.

A trusted `metered_callback` receives keyword arguments `run_id`, `messages`,
`tag`, `max_output_tokens`, `max_usd` and `purpose`. It must reserve the provider
maximum within `max_usd`, then return `{text, call_id}`. Before using the output,
the service verifies a new tagged model-call record, actual usage, the matching
reservation/debit and exact reconciliation. A boolean assertion of metering is
insufficient. Missing billing evidence blocks the operation. Exceeding the
reservation freezes and taints the run. Interrupted cycles are not replayed
automatically, avoiding duplicate billing.

The registered `structural-no-flags@1` validator arm suppresses private flags;
`belief-prose-no-slots@1` permits prose facts without requiring triples. Neither
arm changes ranks or filters which records the agent can read. Known world
injection scores override supplied evidence scores. Agent overrides of the
mechanical revision/update classification are journaled with disagreement.
The private-shorthand flag is a versioned, uncalibrated code heuristic, not a
claim of validated classifier accuracy.

Same-life sessions can cite earlier world evidence and resolve earlier pending
expectations. Late evidence notifications rehydrate their original observations
in the new fold. The idle companion worker runs once per changed world context;
its own model output does not create an endless sequence of new idle triggers.
Forecast questions can bind a real `median_binding` JSON resolver or trusted
`median_reader`; the median is captured on each call with provenance. A static
registered median is explicitly labelled pinned rather than a live observation.

`beliefctl` is the thin JSON/YAML shell client for these same tools. Set
`WORLD_RUN_ID`, `WORLD_BASE_URL` and the scoped `WORLD_RUN_TOKEN`; it sends no
researcher control credential. For example, `beliefctl about harbour`,
`beliefctl history b_...` or `beliefctl revise b_... '{"rank": 2}'`. It requires
the scoped token and preserves a nonzero exit status for a typed block.
`beliefctl about harbour --format yaml` uses safe YAML serialization of the
same response; changing the format never changes the underlying query.
`validate_bundle` can check a donor before creating a destination run, avoiding
an orphan destination if a signature, delta, timestamp or skill hash is invalid.

Free workspace notes remain optional and are never parsed. An evidence reference
of `sha256:<hex>` or `note:sha256:<hex>` resolves an already captured note or
automatically snapshots a matching regular file under the current `/work`.
`register_note(run_id, path, expected_hash=...)` offers the same explicit operation.
The world stamps a receipt and keeps exact immutable private bytes; later edits
or deletion of the workspace file cannot change the citation. Symlinks, path
escapes, hardlinks and special files are rejected. Notes always carry
`self_inference` provenance, including after pattern transfer; a copied page in
a note does not become independent observed evidence. Registration emits no
reflection request, interpretation, or requirement to write notes.

The optional `copula-slots@1` suggester proposes subject/predicate/object slots
only for a limited single-copula or equality grammar. A missing-triple structural
block may include `suggested_slots`; `beliefs(query="suggest_slots", claim=...)`
requests the same proposal. The agent must explicitly submit any accepted slots.
Ambiguous or unsupported grammar gets no suggestion; no model call, truth
decision, rank change, or semantic rewrite occurs. The registered
`slot_suggester_policy_id="none"` arm disables proposals.

## Measurements from exact recorded budget checkpoints

`analysis.belief_checkpoints.belief_checkpoints(world, run_id)` derives folds at
the first recorded ledger debit reaching ten, twenty-five, fifty and one hundred
percent of the configured cap. Unreached checkpoints are unmeasured. Semantic
version audit IDs and imported sequence boundaries, rather than tied timestamps
or the final store, select the actual prefix. Later beliefs and post-seal evidence
cannot leak into earlier checkpoints. Legacy imports without an exact boundary
remain unmeasured.

The helper reports preservation violations, grounded expectation rank bins with
at least thirty resolutions each, and revision latency in actions and dollars.
An explicit delivered `beliefs` query is the available read-observation surface;
private workspace reads cannot be inferred. Revision counts distinguish new
non-self evidence references from revisions without such a new reference; this
code proxy makes no causal evidence-driven claim. Retractions remain revisions
of their prior record. Companion revisions require confirmation before the
observation or edit. Both overall and triple-only counts are available.

`analysis.formation.FormationRunner` separately samples up to two hundred prose
records deterministically, uses budgeted cross-family judge calls on verified
measurement children, reports task-specific kappa, and keeps uncalibrated labels
exploratory. Its canonical-claim Jaccard and tied-rank Spearman calculations apply
the same companion inclusion rule to each seed. Latency is withheld below fifty
percent triple coverage. No judge is asked to decide whether a belief is true.

Graph walks, belief full-text search, RDF export and a consistency oracle are
explicitly deferred by the design. Real model registrations, calibration labels,
cohorts, private canary ownership and external observations must still be supplied
for actual experiments; local tests do not substitute for those measurements.

Tests exercise these algorithms with local transports and metered model
fixtures. They do not execute the multi-day E0/R1 studies or establish character
formation, reflection effectiveness, portability or any scientific stage gate.
