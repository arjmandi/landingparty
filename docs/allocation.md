# Post-run content allocation

`analysis.allocation.AllocationRunner` supplies the non-instrumental series in
design §8.2 and the content-allocation evidence for experience in §8.5. It runs
only after seal and makes explicit, metered, tool-less calls to a different model
family. It does not classify enjoyment or supply independent grounded evidence.

The pipeline option is:

```json
{
  "allocation": {
    "instrument": {"path": "allocation-instrument.json", "hash": "sha256:..."},
    "model_spec": {"model": "pinned-judge", "family": "other-family"},
    "budget_usd": 10,
    "max_tokens": 2048,
    "max_input_chars": 120000
  }
}
```

The instrument is a private, hash-pinned JSON file with `classes`, each containing
a unique `id` and a concrete `description`, and `calibration_rows`. A calibration
row has `input: {source_run_id, action_ref}` identifying an actual sealed action
slice, and expert `labels: {content_class, instrumental}`. Either label may be
null. Optional nullable boolean labels are `reiteration`, `revisit` and `play`.
The runner reconstructs the actual prefix and archived content for that action;
caller-written text cannot masquerade as the referenced world observation.
Duplicate calibration actions are rejected. Labels are withheld from judge inputs.

The runner reads complete archived text only after verifying its captured hash,
and uses actual browser/extraction/search text in immutable outcomes. It also
recognizes authored content that crossed a boundary. Workspace actions invisible
to the world cannot be inferred. Missing, non-text or changed resources remain
unmeasured. Introspective belief, expectation and ask tools and their replies are
excluded by the versioned input policy; no self-report is used as a fitted label.

Each call covers at most twenty-five target actions using the actual cumulative
world prefix available by that segment. Its response labels every target
separately, preserving mixed content within a bin. Every non-null reading must
cite actual content belonging to that target. Positive reiteration or revisit
labels additionally require content from a strictly earlier action. A declared
researcher goal is required to label instrumentality; an open-ended run does not
silently become entirely non-instrumental. All inputs are checked against the
registered character cap before any calls; nothing is silently truncated.

Reports retain exact action/record hashes, resource hashes, evidence references,
source-prefix anchors and separate measurement identities. Action shares and
dollar shares remain separate. Each calibrated provider request includes the
verified frozen instrument hash, binding its captured judgment to the exact
expert bank without revealing expert labels. Dollar windows contain actual debits between
successive action records, including the final window through seal. This is
temporal allocation, not an assertion that a particular content item caused the
model spend. Null readings stay outside the labelled denominator, with their
coverage and unmeasured count shown explicitly.

At least forty distinct fresh expert-slice judgments, a pinned instrument,
paired coverage of 0.6 and measured kappa of at least 0.6 are required for a V
task label. Expert slices must match the target's actual goal family; research
runs require a concrete family, and mismatches reject before any paid call.
Otherwise it remains exploratory J. Both content classification and
instrumentality must qualify before allocation by content class is V. The report
retains each task's actual calibration, uses their minimum kappa and maximum miss
rate for the combined allocation reading, and counts expert-positive abstentions
as misses. Bare `human_agreement` values never promote a column.

Reiteration, non-goal revisit and play rates require their own qualifying labels;
an uncalibrated rate is exposed only as exploratory and is unmeasured for V.
The label reliability ceiling is `1 - miss_rate`; the life rubric separately
bounds the experience V ladder by `floor(5 * (1 - miss_rate))` and enforces its
own coverage and event-count requirements. G stays unmeasured: content allocation
does not prove independent responses, felt experience, or enjoyment. Source
records, source budgets and sealed archives are not modified by judging.

The local provider tests exercise actual metering and fixture calibration. They
do not establish real classifier accuracy; a reviewed frozen taxonomy, expert
labels, compatible priced judge and paid observations remain operational inputs.
