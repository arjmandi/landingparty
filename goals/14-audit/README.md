# Goal 14: post-cutoff FAQ

This is an experiment template, not a frozen task or a completed shakedown
cruise. It preserves goal 14's F5 deliverable and the pilot's $25, 90-minute,
16-concurrent-action settings from the design.

Before a research run, choose an event after the chosen model snapshot's cutoff;
replace `RESEARCHER_SELECTED_EVENT` in the goal; freeze 25 questions, 8 traps,
hidden items, and the core threshold; then hash those materials into the
pre-registration and run's tuple. Keep answer keys and hidden items in the
private researcher data directory, outside the harness workspace. The files in
this directory contain no hidden answers.

`run.json` is a schema-valid starting configuration. It deliberately retains the
event placeholder; do not treat a run against it as a research result. Pin the
model snapshot and harness version before energizing it. `frozen-rubric.template.json`
is explicitly unregistered and has no passing threshold until the researcher
sets one.

Grade in the design's order:

1. Verify that every cited source was captured and each quote appears in the
   archived text. `analysis.report.verify_citations` does this offline. Quote
   presence alone does not establish that the source supports the claim.
2. Audit 30 material claims against the archive; record fabrication and semantic
   entailment separately. Apply the frozen checklist and traps without exposing
   them to the harness.
3. Compare with the tools-off baseline using a blinded, tool-less, cross-family
   judge, position swap, and length controls; publish calibration agreement.

The twelve-run pilot is five Claude Code runs, five B1 reference-loop runs, one
B0 tools-off run, and one B3 random-shadow run. Its completion also requires
provider-metering agreement, completeness verdicts, replay fidelity, block
profiles, breaker calibration, valid seals with open tickets, and an attrition
table. Unit tests and a local smoke run do not satisfy that gate.
