# Entity discovery instrument

The optional §8.3 instrument labels literal named-entity mentions in captured inbound
observations. It makes real, metered, tool-less calls to a pinned model from a different
family than the source agent. It does not infer that a named referent exists, and never
produces a grounded G score. There is no default heuristic or invented entity list.

Use `await analysis.entities.EntityInstrument(world, research_model).run(run_id, config)`
or the post-run pipeline's `entities` option. A minimal configuration is:

```json
{
  "instrument": {"path": "entity-instrument.json", "hash": "sha256:<actual bytes>"},
  "model_spec": {"model": "<pinned snapshot>", "family": "<actual model family>"},
  "budget_usd": 2,
  "max_tokens": 2048,
  "max_input_chars": 120000
}
```

The private instrument artifact contains a named frozen ontology, optional exact aliases,
and expert annotations bound to actual captured text:

```json
{
  "instrument_id": "researcher-ontology@1",
  "classes": [{"id": "ORG", "description": "An explicitly named organization"}],
  "aliases": [{"type": "ORG", "canonical": "Acme Labs", "forms": ["Acme Labs", "ACME"]}],
  "calibration_rows": [
    {"observation_ref": "<actual item ref>", "text_hash": "sha256:<actual text>",
     "labels": ["B-ORG", "I-ORG", "O"]}
  ]
}
```

`entity_source_items(world, run_id)` returns the private source items with exact text,
hashes and observation references for expert annotation. `tokenize(text)` returns the
frozen Unicode `\w+|[^\w\s]` tokens and character offsets. Expert BIO labels must cover
every token, with valid same-class `I-` continuations. Each calibration item must have a
different actual text hash, rather than counting duplicate pages or repeated tokens as
independent items. Other sealed source runs can be identified with `source_run_id` on
an annotation; the judge must be cross-family for each one. Labels are never sent to
the judge. Researcher annotations are trusted inputs; a pinned artifact alone does not
prove expert competence or annotation independence.

The source must be sealed, untainted and reconciled. Every archived `/world/fetched/`
body is checked against its immutable outcome hash. Complete UTF-8 text is read; HTML
removes script, style, template and noscript elements before extracting complete text.
Observed accessibility trees, extracts and search snippets are also supported, including
asynchronous batch-result events. The exact registered tool channels and extraction
policies are reported. Agent thoughts, belief declarations, private browser cookies,
hidden authenticated response bodies and arbitrary outbound text are excluded. Offsets
refer to the derived text, whose own hash is retained, rather than HTML byte offsets.

Each call receives one complete observation, with no later observation or hidden prefix
truncation. All source frames and expert labels are checked before the first purchase;
an oversized frame fails the whole instrument before any call. The explicit total budget
is split into per-call ceilings, including calibration calls. Every accepted result needs
a fresh sealed child ledger receipt and an exact hash-anchored prompt/response capture.
The parent's records and spend remain unchanged. Capture-disabled sources cannot
silently acquire verified entity readings.

Results include literal, nonoverlapping, complete-token spans, citation references,
source/text hashes, model identity, calibration receipts and a discovery series against
actual cumulative source dollars and world actions. Aliases merge only exact declared
forms within the same class; otherwise literal surface and case are preserved. The first
accepted labeled mention supplies an entity's first-touch coordinate. Earlier abstentions
are flagged, so incomplete labeling does not imply a complete earliest-touch claim.

Calibration reports pooled token-level BIO Cohen's kappa and micro precision, recall and
miss rate. Positive precision/recall require the exact class and `B-`/`I-` boundary label.
Kappa uses paired tokens; positive expert tokens in abstained items count as misses.
V requires at least forty distinct actually scored expert items, paired-item coverage at
least 0.6, and measured kappa at least 0.6. Every other result stays J. Bare agreement
claims, unpinned arrays, a large token count, or a model's confidence cannot promote it.
An explicit empty span list is a scored no-mention reading; null abstention remains
unmeasured and never becomes a zero discovery point. The software tests use local
fixture annotations and provider responses; their passing calibration does not establish
scientific validity for a production model or ontology.
