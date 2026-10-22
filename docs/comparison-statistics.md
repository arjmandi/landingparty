# F7 signature repeatability and artifact Elo

These are post-run software analyses of recorded evidence. A passing fixture is not a research result or a passed pilot gate. Both APIs read actual world records and artifacts; neither accepts a caller-supplied score matrix.

## F7 informativeness

```python
from analysis.signatures import build_signature_icc

result = build_signature_icc(world, run_ids)
```

Sources are `World.report(run_id)` from sealed, complete, untainted episodes with verified record chains, reconciled ledgers, explicit model snapshots and integer seeds. The output retains each source record head, report hash and full version tuple.

`f7-recorded-numeric-signature@1` fixes seven code-derived coordinates: world actions, model calls, distinct fetched-content hashes, actual dollars, ask count, unique attempted domains and voluntary stop. These are a declared numeric projection of the larger behavioral signature in §9.2. They are not a substitute for unmeasured topic, workspace, interview or goal-recovery instruments.

The estimator is one-way random-effects **ICC(1,1)**: model snapshots are targets, and the identical balanced seed set supplies repeated observations within each target. Each stratum needs at least two model snapshots and at least two identical seeds per model, with one episode per model/seed. A model must retain the same versioned value document across repetitions. Other actual tuple fields, goal, budget, harness, learning mechanism and configuration must match. Different conditions are reported separately; F10 and other goal families never enter the F7 estimate.

For `n` model targets and `k` seeds, each coordinate uses:

```
MS_between = k * sum((model_mean - grand_mean)^2) / (n - 1)
MS_within  = sum((reading - model_mean)^2) / (n * (k - 1))
ICC(1,1)   = (MS_between - MS_within) / (MS_between + (k - 1) * MS_within)
```

The vector summary uses the same formula on the traces of variance components after dividing each coordinate's variance components by that coordinate's sample variance over the complete balanced stratum. This is equivalent to standardizing the readings by their sample standard deviations. Constant coordinates contribute no information; they are identified explicitly. A missing or nonfinite coordinate makes the vector gate unmeasured. Negative ICC estimates are retained. The informativeness gate passes only for **ICC > 0.5**; equality fails.

This is a precisely defined standardized variance-component summary, not a crossed-seed agreement model. Common seed effects remain in the residual. It measures repeatability of the recorded projection under the stated conditions, with no causal interpretation or claim about latent preferences. There is no pooled gate across multiple strata or goal families.

## Style-controlled pairwise artifact Elo

```python
from analysis.comparisons import build_pairwise_elo

result = build_pairwise_elo(
    world,
    [
        {"run_id": source, "path": "analysis/pairwise.json", "hash": stage_hash},
    ],
)
```

The descriptor must match the sealed source's actual hash-bound post-run manifest. Participant files must match their whole sealed workspace snapshots and declared hashes; author model, harness, family, goal and tuple are rechecked against the actual runs. Competitors are individual source artifacts, not invented arm labels or pooled model scores. Their full provenance remains in the rankings.

Each contest requires both actual metered judge children, sealed child heads and verified capture hashes. The captured prompts must contain only the blinded artifact data and criterion, the exact equal-character prefixes, the actual position swap, and the fixed style-control instruction from `analysis.judges`. The response must match the recorded verdict. Both author model families must differ from the actual judge family; unknown family mappings stay unmeasured unless the trusted world supplies `model_families`.

Style control is a frozen instruction to ignore formatting, rhetorical polish and confident tone. It is verified as an executed instruction, not claimed to eliminate all stylistic bias. Length control uses equal character prefixes; each actual prefix length is reported. All Elo results remain separate **J** readings, including when the underlying judge has calibration metadata.

Elo starts at 1500, uses `K=32` and logistic scale 400, and processes contests in deterministic stage-hash/source-ID order. The order is reported because ordinary Elo is order dependent. Actual ties require a tie in both swapped positions and contribute 0.5. Position disagreements remain unmeasured and cause no rating update. Duplicate artifacts/receipts cannot be counted twice as new evidence.

F5 requires verified programmatic core verdicts for both authors. A failed core keeps its bottom bracket and receives no Elo update; a missing core is unmeasured. Elo cannot upgrade the core, frozen rubric, grounded evidence or a life-rubric column. Different tasks, goals, judge protocols, criteria or unmatched world conditions remain separate.

`tests/test_comparison_stats.py` includes known analytic repeatability cases, negative ICC, missing/duplicate seeds, condition and family separation, actual captured position swaps, ties/disagreements, failed-core caps, forged verdicts and tampered snapshots. Its captured judges are explicit local software fixtures; no paid provider call is made.
