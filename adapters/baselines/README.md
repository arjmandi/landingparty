# Baseline harnesses

`python adapters/baselines/runner.py BASELINE run.json` uses the same world API
and dollar ledger as the reference loop.

- B0 supplies no tools, disables the world's tool manifest, and makes one raw
  model draft plus twenty revision turns by default. Model calls use the lane.
- B3 is a deterministic random shadow over registered seed URLs, queries, and
  the read-only core surface.
- B3b is a bounded breadth-first crawler following links from captured pages.
- `self-interaction` is the tools-off repeated-dialogue attractor control.
- B3c is a Python API only: it requires its own registered life and explicit
  recipients, then submits templated `send` calls through the world's ordinary
  stage, lease, identity, first-contact approval, and disclosure checks.

Step limits, revision turns, and seed are part of harness identity. No null is
automatically run, no process bypasses the gateway to retrieve pages, and no
baseline result is declared a completed pilot. B4 is the researcher's separately
recruited human verifiable slice; this code does not impersonate that baseline.
