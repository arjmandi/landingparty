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
An asserted `human_agreement` does not promote a judge. Supply
`judge.calibration_artifact:{path,hash}` with frozen expert rows and a separate
`calibration_budget_usd` inside the total research cap. Each row has `task`
(`citation_support`, `goal_recovery`, or `ask_classification`), `input` using
that task's normal data fields, and expert `label`. The same actual prompts run
in fresh metered children; the runner verifies every receipt and computes
task-specific agreement and per-class precision/recall. Forty labelled items
and kappa at least 0.6 promote only that task. A derived label remains J without
this evidence; pairwise preference remains J.
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
never headline, and no consistency score is manufactured. Structured answers
retain the actual 0–100 reality rating, parameter self-ratings, and per-day
ratings; omitted or invalid numbers remain explicitly unmeasured. Reality belief
does not become an inferred score from the prose.

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

## Actual code grading and the F5 bracket

`grading_protocol:{path,hash}` enables `GradingRunner` on the verified sealed
workspace snapshot. Research series must pin the exact descriptor hash in
`prereg.grading_protocol_hash` before the episode. The protocol supplies
`protocol_id`, `core_threshold`, and `checklist` rows containing `item_id` plus
a `check`. Built-in kinds are `file_exists`, `json_equals` (JSON `pointer` and
`expected`), `sha256_matches` (`expected` hash), and `citation_quotes`; every
input `path` starts with `work-snapshot/`. Missing files produce observed failed
checks. Changed snapshots, escaping paths, symlinks, and workspace code execution
are rejected. Each observation cites the actual sealed snapshot record.

Optional `hidden_items:[{item_id,answer}]` and `answers_path` execute the fixed
answer key with correct +1, incorrect −2, and abstained 0. Keys are private
world inputs. `claims_validator:true` enables the epistemic claims file at
`claims_path`; only host-registered `pipeline.register_claim_checker(id,callback)`
callbacks run. Unknown checkers remain unmeasured. A failed frozen programmatic
core caps F5 at the bottom bracket and buys no pairwise judgments. Missing or
incomplete core evidence cannot silently enter the F5 preference bracket.
Optional `goal_metadata` is validated against the exact treatment text and
included in the spec sheet. `analysis.goals.validate_goal_protocol` enforces the
section 9.1 fields; draft templates and null human-time/messiness values do not
become registered metadata.

## Portable mission reports

`lp report` and `/v1/report` can render JSON, Markdown, CSV, or standalone HTML.
Every CSV reading carries its run/life identity, policy, price-table, and preamble
identifiers. The spec sheet contains the actual conditions, verbatim seed scope,
budget vector, cost/token decomposition, completeness, attrition, goal metadata,
and calibration inventory. Measured values are shown beside explicit missing
reasons. Sponsor readings remain separate. Crowd G, each calibrated/un-calibrated
critic V/J, and agent prediction S stay in different columns; their residuals
do not turn into a single taste score.

Every post-seal pipeline emits `analysis/publication/` with JSON, Markdown,
CSV readings/curve points/attrition, SVG and PNG figures, a self-contained HTML
report with embedded SVG, and a file-hash manifest. Only actual points are
plotted: each trajectory has separate dollar and world-action panels. A missing
axis omits the figure with a reason. Life checkpoint publications add calendar
days, a four-column rubric matrix, and the actual coverage accrual curves.
They live at `lives/<id>/publication/<checkpoint-hash>/`. Passive readers verify
the matching report and figure hashes; they make no model calls or new source
observations. Never interpolate a missing checkpoint into a measurement.

`horizon_failures` with a pinned `horizon_model` enables the actual cross-family
25-action cumulative-prefix labeler for thread abandonment, commitment
contradiction, and repeated rediscovery. The optional `instrument` descriptor
contains frozen calibration rows. Input caps are checked before purchase;
fresh sealed child receipts and actual expert calibration determine each
metric's J/V column separately. Null labels remain missing rather than false.

## Pilot readiness evidence

`POST /v1/studies/<id>/readiness` evaluates the actual registered shakedown via
`PilotReadiness.evaluate`, with optional `price_table_artifact` and
`probe_bank_artifact` private run-owned descriptors. It makes no paid calls and
does not register a gate. Provider agreement is independently repriced from the
provider's raw usage report in each actual `model_capture`, bound to the terminal
source record and compared with nanodollar ledger settlements. Capture off,
unknown usage, changed bytes, missing frozen prices, or a model baseline with no
calls cannot pass. The bank must contain at least sixty actual keyed items, with
its exact hash frozen in `prereg.probe_bank_hash`.

The evaluator also derives the exact twelve-run schedule and all started
casualties, completeness verdicts, source dollars/actions per harness, actual
replay-index fidelity, verified contemporaneous 300-URL block profiles,
chain/ledger/seal checks including an actual open-ticket seal, and counterfactual
breaker rates from runs whose hashed policy actually used `mode:log_only`.
The proposed empirical thresholds are recorded and never automatically applied.
The goal/questions/traps and complete goal metadata must already be frozen.
Readiness artifacts are immutable under the study; `PilotReadiness.verify`
recomputes their actual evidence without writes. Registry gate registration
requires that exact artifact and actual research provenance. A twelve-run
software fixture remains development evidence and cannot unlock research.

## Historical curves and whole-life cohorts

The runtime records bounded private workspace checkpoints after the first actual
committed debit crossing 10%, 25%, 50%, and 100% of the source dollar cap. The
post-seal `grading_protocol` grades these verified historical bytes using the same
frozen core as the final artifact. `curve_score_path` defaults to
`["checklist","pass_fraction"]`. Missing pre-restart observations or bounded-copy
omissions stay unmeasured. A debit crossing several thresholds contributes one
observed state at its actual overshoot coordinates. Vertical changes at repeated
coordinates contribute zero area; the last actual observation at that coordinate
is used, with the policy and all raw receipts retained. Both dollar and action
log-AUC/slope are published. No final artifact is copied into a historical score.

`StudyReport` consumes actual registry execution rows, frozen primary G/V paths,
source completeness and comparability before pooling distributions or `pass^k`.
Carryover contrasts re-resolve the preregistered dependency graph and actual
imports/archive receipts. Whole-life studies count each durable life/cell/seed
join once. Sleeping and resumed sessions remain in costs and attrition, never as
independent repeats. Failed or uncertain requested measurements stay visible.

## Content, entities and rubric shapes

`allocation` enables the cross-family content/instrumentality classifier with a
hash-pinned `instrument`, explicit `model_spec`, `budget_usd` and input caps.
The instrument contains described `classes` and expert `calibration_rows` tied
to actual archived action slices. Each captured request binds the instrument
hash. Forty fresh paired expert slices and actual kappa determine V; otherwise
labels remain exploratory J. See [allocation.md](allocation.md). Source and
life publications include separate content/non-instrumental action-share series;
life series preserve actual cumulative dollars/actions/calendar coordinates and
never combine different protocol hashes.

`entities` enables `EntityInstrument` with a frozen BIO ontology/alias protocol
and the same explicit budget controls. Literal archived spans, token offsets,
actual first-touch coordinates and calibrated J/V are preserved. Unknown names,
changed bytes and incomplete context cannot silently increase a discovery count.
See [entity-instrument.md](entity-instrument.md).

A life binding can declare `classifier_mappings`, for example
`[{"parameter":"experience","label":"play","evidence_class":"play_artifact"}]`.
Supported labels are `play`, `reiteration`, `revisit`, `instrumental`,
`non_instrumental`, and `content_class`; `equals` selects the actual label,
`content_class` optionally restricts the category, and `value:"interval_usd"`
selects actual attributed dollars rather than event count. The life runner
reconstructs frozen expert labels, actual prompts/responses, unique child
receipts and bills before using saved allocation evidence. Claimed scalar
kappas are rejected. The minimum actual task kappa and maximum actual miss rate
bound experience V; sponsor/probe labels never become G.

Research lives additionally require `life_rubric_policy_hash` and
`life_binding_hash` in their actual pre-session series preregistration. A rubric
policy may define `shapes:[{name,all:[...]}]`. Each predicate names `checkpoint`
(`current`, d7/d14/d28/end/after30/after90), `parameter`, `column` G/V/J,
`field` (default `value`; also `mean_rating`, `rate`, or `n`), `op` gte/lte/eq,
and finite `threshold`. The seven design names use underscores, such as
`pleasant_executive`. Only registered thresholds are applied. Missing readings
and overlapping matched rules remain unmeasured. Earlier matrices come only
from hash-verified registered checkpoints, with future readings excluded.

Whole-life cohort reports also join actual registered d7 mean J ratings to
completed after90 G ladders per parameter. Strict matching precedes Spearman
correlation, individual pairs and missing reasons remain visible, the forty-life
L1 floor is explicit, and J-based predictive results never become automatic
scientific gate evidence. Scheduled judge-pair health checks require an explicit
frozen protocol and per-check/lifetime budget; their agreement is drift evidence,
not expert calibration.

A delivered `work-snapshot/out/self-assessment.json` (or explicit
`self_assessment_path`) is retained with its actual sealed snapshot/hash as S,
never as a checked grade. Frozen-mode spec sheets verify the actual pinned
corpus bytes/manifest and report local search fidelity against archived query
lists. Missing query coverage stays unmeasured; this is not a claim that a
historical snapshot is identical to the live web.
