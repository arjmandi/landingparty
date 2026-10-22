# Running research after a sealed episode

`analysis.pipeline.PostRunPipeline` reads a sealed run, its captured resources,
and its workspace snapshot. It writes private artifacts under
`data/runs/<run>/analysis/`. It never reopens the source run or appends after its
terminal seal. The normal report keeps dollars and world actions as separate
axes; internal browser/API subrequests are recorded as requests without counting
them again as semantic world actions.

`POST /v1/debrief` accepts `{"run_id":"...","options":{...}}`. With empty options
the pipeline performs record/ledger verification and archived quote checks if
`work-snapshot/out/claims.json` exists. This makes no model calls. A quote found
in captured text is explicitly separate from semantic support for the claim.

Paid stages require a pinned model configuration and an explicit total cap:

```json
{
  "measurement_budget_usd": 5,
  "agent_family": "REGISTERED_AGENT_FAMILY",
  "judge": {
    "model": "PINNED_CROSS_FAMILY_JUDGE",
    "family": "REGISTERED_OTHER_FAMILY",
    "provider": "openai",
    "budget_usd": 1,
    "max_tokens": 1024,
    "prompt_id": "registered-judge-prompt@1"
  },
  "semantic_citations": true,
  "goal_recovery": true,
  "ask_classification": true
}
```

`budget_usd` reserves the maximum of each call or pairwise operation; the total
cap covers all requested stages. The existing lane refuses unknown model
prices, unsupported hosted web tools, and requests the remaining cap cannot
cover. Every research call uses a separate world-owned child run, priced in
`judge` or `probe`, with its actual model snapshot and a terminal seal. Source
agent dollars remain unchanged. Ambiguous failed calls retain their conservative
research-budget reservation. The pipeline reports partial failures and continues
independent stages; it never replaces a failed instrument with a passing score.

Judges are tool-less and must have a different declared model family than the
agent. Pairwise judging swaps positions and uses equal-length character prefixes,
retaining disagreement. That length-control method is recorded, not hidden.
Goal labels use 25-action segments; asks are classified only after the run.
`human_agreement` and `calibration_set_hash` identify independently calibrated
judges. A derived label remains J below kappa 0.6; pairwise preference remains J.
Process judging requires replay mode. Calibration helpers publish kappa,
precision, recall, and miss rate rather than assuming the judge is correct.

## Completeness, interviews, and probes

Options `transcript_path` and `shell_audited` enable the cooperative completeness
audit. Paths must remain inside the source run. The parser deduplicates Claude
stream chunks by message id, counts compactions, detects malformed records, and
flags network-capable or proxy-bypassing shell commands. It stores command
hashes instead of raw commands. Complete requires matching lane/transcript call
counts and explicit shell-audit evidence; a nonempty record alone proves nothing.

The `interview` option uses the agent's exact pinned model and an explicit budget.
Free recall receives only the read-only workspace snapshot in a fresh context.
Day reconstruction is a separate call with the record grouped by day. Both are S,
never headline, and no consistency score is manufactured.

`probe_bank`, `probe_model`, and `probe_mountings` run independent contexts for
`pre_off`, `pre_on`, `post_off`, `post_on`, and `post_mem`. Only read tools
(`search`, `fetch`, `fetch_many`, `extract`, `inbox`, `status`) are available in
the on cells. The store-only `post_mem` mounting rejects transcripts and episodes.
Bank answer keys never enter the prompt. Items exposed by any source-run URL,
including batch and final redirect URLs, are excluded across all five cells.
Gains use paired item ids, with own-situation strata reported separately.

The default measurement child replays its source archive. A held-out source may
therefore be a replay miss; that does not establish live-world probe validity.
`probe_world` or per-cell `probe_worlds` select trusted `mode`, `archive_in`,
`corpus_id`, and `frozen_at` conditions for an available corpus or live probe lane;
the child tuple records what was actually used. Accuracy gaps alone are not a
positive reset test. Unspecified post mountings load the actual captured context
or signed belief export; a missing captured context leaves that post cell
unmeasured. Researcher-supplied mountings retain a distinct provenance label.

## Lives and empirical gates

`PostRunPipeline.t0` runs the lottery, leisure, persona, and attractor battery in
fresh metered contexts, anchored to a sealed preflight run with the same pinned
model. A completed lottery needs a trusted real-stake settlement callback; a
fictional-money question is insufficient. The published model value document
must have a captured body matching its pinned hash. `LifeService.record_t0`
checks the actual sealed probe child runs before registering the fingerprint.

`analysis.rubric.score_life` evaluates registered event anchors for connection,
purpose, contribution, and experience at d7, d14, d28, end, after30, or after90.
The design leaves numerical anchors for pilot calibration, so the caller supplies
a versioned policy. Coverage is at least 0.6 with at least five events per evidence
class. Sponsor, sibling, and probe readings never enter G. Arena readings need
unsolicited return or after-refusal evidence. Experience V is bounded by the
calibrated labeller's miss rate. Missing parameters remain unmeasured; aggregates
are minima only and require every parameter at the appropriate stage.

`analysis.experiments.score_r1` requires two pinned models, at least five
registered seeds each, and at least forty paired forced choices per sample.
It reports run-bootstrap intervals for store, retest, full-context, and shuffled
controls. Positive requires store-minus-retest at least 0.15 and non-overlapping
store/control intervals for each model. A development fixture can exercise this
calculation without becoming scientific gate evidence. RegistryService also
verifies actual sealed research runs, registered preregistration, exact tuples,
stored report artifacts, and integrity.

`ExperimentRunner.execute` interleaves real harness callbacks and retains every
started run's attrition. Use one registered series per pinned cell tuple;
different harnesses or models use separate series sharing the study's prereg.
The runner never silently weakens tuple equality or drops failed runs.

`power_table` supplies a paired-normal approximation from the registered effect
and pilot standard deviation, with its assumptions and minimum seed floors
explicit. It does not substitute an invented pilot variance. Binary expectation
calibration reports Brier and expected calibration error only with thirty
resolutions, stratified by resolver and venue. Revision latency requires matched
world observation references and uses action/dollar coordinates, never wall time.

`goals/catalog.json` contains all fifteen goal configurations. `prereg/` contains
experiment templates and six domain bindings. These are configurable assets,
not authored expert keys, frozen scientific registrations, or completed studies.
Real event selection, hidden expert labels, calibrated anchors, power tables,
provider credentials, elapsed lifetimes, and afterlife evidence remain actual
prerequisites. Pass^k means reliability that all k trials succeed, includes every
started run, and enforces the design's minimum seed counts.

`PostRunPipeline.configure_life` freezes the rubric policy, domain binding,
and optional calibration from hash-pinned files in a sealed session belonging to
that life. `PostRunPipeline.life(life_id, checkpoint="end")` scores the configured
inputs and writes an immutable, hash-named artifact under the life directory;
LifeService anchors its reference in the life event chain. The control endpoint
is `/v1/life/rubric`. The default evidence source is the actual footprint ledger,
using the frozen binding's `evidence_mappings` (parameter, column, evidence_class,
item_kind, optional reading_kind/value_key). Missing mappings produce unmeasured
parameters. Explicit evidence artifacts use the same verified-reference format as
trajectory instruments; G rows must preserve the real footprint author and
unsolicited/return/refusal provenance. Sponsored observations remain separate.

Checkpoint execution requires a sealed active session, the actual calendar date,
and, for end or afterlife readings, the actual life end or completed scheduled
reading. Only evidence available by the checkpoint enters its score. Configured
after30/after90 scoring runs after LifeService's real footprint reading and never
reopens the source run record. A scoring failure can retry without repeating that
reading. Files are frozen research inputs; neither registration nor a software
fixture establishes calibrated psychological anchors or completed empirical gates.

## Trajectories and measured instruments

Every passive report includes phase spending, domain/document discovery,
tool counts, revisits, and token ratios from the source record. The post-seal
pipeline also measures the captured workspace's size. Semantic thread abandonment
stays unmeasured without a calibrated labeler.

Optional trajectory instruments use `instrument_artifacts`, a mapping from
`cost_performance`, `decision_divergence`, `open_endedness`,
`verifiability_accrual`, `predictive_validity`, or `risk` to
`{"path":"instruments/checkpoints.json","hash":"sha256:..."}`. The private
file contains `{"rows":[...]}` in the corresponding `analysis.trajectory`
function's schema. Paths remain inside the sealed source run. Each observation,
context, and receipt reference must identify an actual source record's hash,
action/event/call id, `record:<seq>`, or a named entry in `reference_artifacts`.
Those entries are separately hash-pinned file descriptors, optionally carrying
`run_id` for an actual sealed child measurement run linked to this source.
Files require valid records and cannot come from unrelated or tainted runs.

Open-endedness accepts `baseline_rows` mapping B3 and B3b to raw measured rows;
it computes baseline slopes itself. Aggregate baseline claims are rejected.
Decision divergence retains all twenty samples for each information-matched
context and requires two hundred decisions for its measured status. Predictive
validity excludes sponsor readings and requires actual matched day7/day90 pairs.
Risk metrics exclude paper money and unsettled requests. Missing checkpoints,
observer losses, or delayed readings are never interpolated into observations.
Reference integrity does not establish the validity of an instrument or its
calibration: these outputs remain non-gating research artifacts.

## Values, taste, and prose formation instruments

The `value_extraction` pipeline option supplies a cross-family JudgeSpec,
per-call `budget_usd`, and hash-pinned `instrument`. The instrument defines
`instrument_id` and `axes` with unique `id`, `description`, `low`, and `high`
anchors. A projection is an anchored integer from -2 through 2 or null per axis,
with references to evidence actually in that context. The runner projects only
record prefixes at reached 10/25/50/100 percent of the source dollar cap;
unreached windows stay unmeasured. It never gives an early window the final
workspace. It compares against the actual registered t=0 battery, its attractor,
its hash-verified published model value document, and the last independent
inbound message available at that prefix. Missing controls remain explicit.
The control exclusion screen identifies movement toward a counterparty or a
known attractor; it never interprets a projection delta as causal formation.
An optional frozen `calibration` slice contains expert `positions` and `evidence`;
only the actual metered labels of that slice determine kappa and J/V promotion.
Every extraction requires a new verified, sealed judge child receipt.

The `taste` option supplies `input:{path,hash}` containing `rows`, optional
`critics` JudgeSpecs with per-call budgets, and a frozen `criterion` descriptor.
Rows keep `item_id`, `t`, `crowd`, `critics`, and `self_prediction` distinct.
Crowd observations must match actual footprint readings. Sponsors, siblings,
and probes remain separate from independent reception. Model critics receive
only the artifact and criterion; they see neither author metadata nor reception
or self-predictions. Critic scores and disagreements remain separate by critic.
Forty actual expert calibration examples plus frozen `score_bins` determine kappa;
an asserted agreement in a model specification cannot promote an uncalibrated
panel. The residual trend fits each critic against crowd reception and reports
the time trend in the residuals, with its method and sample size.

An empty-row taste run on a sealed preflight anchor emits `critic_calibrations`
for panel preparation before the first life session. A later run can supply a
hash-pinned `panel_calibrations` artifact (with its anchor `run_id`); the runner
rechecks every distinct child receipt, exact criterion/slice hash and actual
pre-first-session timing, recomputes agreement, and reuses those paid readings.
Post-hoc calibration cannot impersonate a panel prepared before the life.

Self reception predictions need source references preceding the actual outcome.
An optional `prediction_model` uses the same pinned agent snapshot in a fresh,
tool-less no-world context with only the artifact and criterion. Its probe
receipt is checked separately. Brier and the item-paired agent-minus-no-world
Brier delta require thirty resolved observations. Missing crowd, panel, or
prediction data never become a single invented taste score.

The `formation` option supplies a cross-family JudgeSpec, total stage budget,
and pinned `input` document with beliefs, revision observations, dropped goals,
optional calibration rows and cross-seed stores. The runner selects at most 200
prose records deterministically and reports whether this is a full sample,
small-population census, or incomplete reading. Default coherence comparisons
form a bounded ring over that sample; they do not claim exhaustive pair checking.
It labels revisions and dropped commitments with explicit uncertainty, publishes
actual task calibration and per-label precision/recall, and gates latency below
50 percent triple coverage. Cross-seed stability uses lexical canonical-claim
Jaccard and tie-aware rank Spearman. Every judged row requires a new sealed,
reconciled child measurement receipt; development data never unlock a gate.

`observer_run:{instrument:{path,hash}}` runs the reproducible byte observers on
a pinned `stream` of private source artifact descriptors with actual
`observation_ref` identities, two distinct `observer_specs` (order, alpha,
background descriptor), and explicit `(t,T)` coordinates. Observer identities
must match the source tuple's combined observer hash or be explicitly labelled
exploratory. These are named byte-ngram observers, with real prediction losses;
they are not presented as pretrained language-model observers.

Life reports additionally load verified, registered immutable control checkpoint
artifacts through `controls:{B0|B1|B3b|B3c|global_control:{artifact_ref,artifact_hash}}`.
Lists retain separate references. Dollar/calendar caps, checkpoint exposure,
binding, policy, model, seed, complete tuples, and actual block profiles must
match. Tuple differences require exact `registered_contrasts` declared in the
actual registry before the life began. Raw t=0 choices are not silently
converted into commensurate rubric scores; absent deltas remain unmeasured.
