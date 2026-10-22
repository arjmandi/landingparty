# Decision-level divergence

`analysis.decision.DecisionDivergenceRunner` executes the design's actual
information-matched copy experiment after a source run is sealed. It does not
run automatically, contact a vendor directly, or charge the original agent.
The caller supplies the same metered `ResearchModel` used by the post-run
pipeline and an explicit measurement budget. The pipeline's shared budget
wrapper remains the authority across paid stages.

The source must have a valid, untainted terminal record, `capture_model_io`, a
pinned model snapshot, and at least 200 captured, classifiable decisions with
20 prior tool-result observations. Insufficient captures produce `unmeasured`
without model calls. Redacted captures are used as actually retained; this
instrument cannot recover information that was never captured or has expired.

Provide hash-pinned private JSON artifact descriptors (`path`, `hash`, optional
`run_id`) for both the action-class bank and registration. A minimal bank is:

```json
{"bank_id":"tools@1","classes":[
  {"class_id":"retrieve","description":"Read another source.","tool_names":["fetch","search"]},
  {"class_id":"respond","description":"Write a final response.","assistant_text":true}
]}
```

Use the actual captured tool names, including harness prefixes where present.
Each tool name belongs to one class. Mixed parallel classes require an explicit
`parallel_class_id` in the bank; unmatched decisions are excluded and counted.
The source's chosen class is determined by this frozen mapping. The copy picks
its own next class; it does not grade whether the original decision was good.
Chat Completions, Responses, and Anthropic captures and their supported SSE
streams are decoded. Unstructured tool output pasted into ordinary user text
cannot be reliably identified as an observation and is not silently guessed.

The registration binds `protocol_id`, timezone-aware `registered_at`,
`model_snapshot_id`, `class_bank_hash`, `decision_count: 200`,
`samples_per_context: 20`, `contexts: [0,5,20]`, `call_budget_usd`, `max_tokens`,
and integer `seed`. Its timestamp must precede source creation. This timestamp
and hash check is an integrity check; the researcher must separately establish
that registration actually preceded collection. It never unlocks research gates.

```python
result = await DecisionDivergenceRunner(world, budgeted_model).run(
    source_run_id,
    model_spec={"model": pinned_snapshot, "family": model_family, "provider": "openai"},
    class_bank=bank_descriptor,
    registration=protocol_descriptor,
    measurement_budget_usd=explicit_stage_cap,
    seed=registered_seed,
)
```

The cap must cover all 12,000 call reservations before work starts. Each draw
uses a fresh tool-less completion with only the fixed goal, frozen class
choices, and exactly the last k captured tool results. The source's assistant
reasoning, earlier conversation, unselected observations, actual chosen class,
and future outcomes are withheld. Selection is seeded without replacement.
Individual provider draws have recorded seed labels; the current
`ResearchModel.complete` interface does not enforce provider sampling seeds.

Private context files and durable draw journals bind context hashes, selected
observation references, response hashes, measurement-run receipts, budgets,
and classes. Completed draws are reused without further calls. An interrupted,
malformed, or ambiguous draw is not automatically resampled: its journal requires
operator reconciliation. This prevents rerunning an inconvenient class or
silently duplicating uncertain paid work. The runner preserves the source's
sealed record and returns an artifact accepted by `load_instrument`.

The result reports `1 - count(original_class)/20` at each k, without inventing
outcomes for unresolved decisions. Optional `grounded_receipts` is a pinned
JSON map from model call IDs to actual footprint reading IDs. Registration must
then contain `vindication_policy` with
`kind: "independent-footprint-threshold@1"` and a numeric `threshold`. The
reading must have independent provenance (or an eligible unsolicited returning
arena participant), numeric observed value, external evidence, and belong to
an action in the decision's recorded action interval. Sponsor, sibling, probe,
and unrelated-action readings are rejected. A missing receipt stays unresolved.
This is a declared uptake-threshold split, not an inferred semantic-success
judgment; concurrent harnesses need review of chronological action attribution.

Engineering tests execute the full 200 × 3 × 20 procedure with an injected
model and no paid calls. Actual sampling, independent outcome measurement,
observer calibration, and scientific protocol review remain research work.
