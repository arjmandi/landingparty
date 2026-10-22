# Runtime follow-through audit

This maps the additional implementation audit to the compiled design. The
original design is unchanged. Scientific completion under §11.4 requires actual
studies; local transports and accelerated clocks establish software behavior.

| Design requirement | Implementation and regression evidence |
| --- | --- |
| §3.1 truthful briefing and seed scope | `RunConfig.seed_scope` and `World._preamble` state the actual seed boundary. Seed labels do not claim deterministic live web or provider sampling. `test_design_followthrough` checks the saved preamble and inducing-condition identity. |
| §3.2/§4.1 alternate search | `worldd.search` provides Brave and fixed Exa neural search. `ProviderBoundary.submit_search` reconstructs the exact request from the recorded action, verifies the held price, and archives the private response. `test_search_providers` exercises both protocols, pinned gateway transport, forged requests, charges and replay without credentials. |
| §3.6 ask context provenance | `worldd.context.ask_provenance` verifies the captured model exchange, matches the actual ask, and extracts only structured observation references in that model window. Missing or ambiguous captures are explicit; broker-observed history is a separate field. `test_context` covers JSON/SSE providers, compaction, duplicate calls and tampering. |
| §3.10 stasis and continuity | `World.push`, `LateEvents`, atomic `Store.push_event_once`, and `LifeService.recover` retain messages arriving during the sleep/seal handoff. Source inbox cursors reconstruct a lost cross-database write; acknowledgement loss cannot duplicate delivery. `test_full_lifecycle`, `test_lives` and `test_storage` cover both crash windows. |
| §4.3/§4.10/§7.2 sealed workspace | Descriptor-based snapshots record omissions and a sealed file manifest. Imports verify the donor anchor before creating the recipient; unsafe links and changed snapshots fail. Exports include the snapshot rather than mutable work. Cross-user writable work is separate from the private original. `test_workspaces`, `test_design_followthrough` and the actual Docker workspace check cover these boundaries. |
| §4.4/§7.1 model capture | Exact capture bytes and request/response hashes are anchored before lane draining completes. Paused streamed calls cannot add a capture after the seal. Actual upstream origin and transport provenance distinguish fixtures from eligible live observations. `test_lane` covers accounting and the stream/seal race. |
| §5.3 complete price identity | The central price identity includes model, search/browser and declared perception prices. Each run freezes its perception provider/model/rate map; availability of a credential is excluded from the price. Quotes bind the source, selected pages/options and that map before reservation. |
| §6.4 signed browsing arm | Stage-2+ research defaults to signed browser identity. An explicit unsigned arm remains a distinct condition; missing signing configuration cannot silently activate signed research. `test_design_followthrough` checks both cases. |
| §6.6 pilot breaker calibration | `log_only` records counterfactual loop/spend interventions without freezing the pilot or sending advice. Hard budget, exfiltration and kill boundaries remain enforced. `test_design_followthrough` covers repeated outcomes and `analysis.programme` derives calibration from actual records. |
| §7.6 comparability | Implementation identity hashes packaged source/resources and actual dependency versions. Forecast, runtime, life and kernel protocols have explicit tuple identities; stable life identity excludes changing readings and generated IDs. `test_design_followthrough` and `test_registry` cover changes and strict series admission. |
| §8.3 horizon failures | `analysis.horizon.HorizonRunner` uses verified sealed prefixes and fresh metered cross-family children. It verifies referenced observations and actual calibration calls, keeps uncalibrated results in J, and leaves the parent unchanged. `test_horizon` checks actual lane calls, calibration, bounds and invalid references. |
| §8.3 asks and blocked responses | Actual UTC-week progress observations retain their S column and missing values. Silence/rejection and block responses use the next recorded world request, exact tool/argument equality, actual dollars/actions and separate inbox-read timing; no intent or awareness is inferred. `test_metrics`, `test_trajectory` and `test_design_followthrough` check these boundaries. |
| §8.3 import and ticket observations | `workspace_import` binds both copied and original verified manifests to the donor seal; `action_ticket` records both synchronous ticket and asynchronous job returns. Reports do not infer receipt from an asynchronous outcome. `test_design_followthrough` exercises actual copies and timed asynchronous returns. |
| §8.7 judge drift | `JudgeCanaryService` claims authorized schedules durably, meters actual swapped cross-family calls, preserves uncertain purchase ceilings and reports actual dates/drift. Reading a report never buys a new check. `test_judge_canaries` verifies scheduling, recovery, tampering and passive report assembly. |
| §8.8 usable reports and exports | The report API and `lp report` support JSON, Markdown, HTML and CSV. Run and life exports retain their authoritative manifests and integrity results, exclude vault material, and include captured I/O only under the run's declared capture flag. `test_cli_full`, `test_publication`, `test_full_lifecycle` and `test_design_followthrough` exercise these contracts. |

The cohort and instrument audit is in [design-build-audit.md](design-build-audit.md).
Gateway/preflight and actual deployment procedures are in
[gateway.md](gateway.md) and [deployment](../deploy/README.md). The final measured
validation scope is recorded in [verification.md](verification.md).

The remaining operating prerequisites include reviewed prices and terms,
world-side egress compatible with destination pinning, securely configured
provider credentials, frozen instruments and calibrated expert labels, owned
identities/venues, approved budgets and applicable consent/IRB decisions. The
12-run scientific shakedown, R1, 14-day life/day-30 reading and 90-day company
study cannot be replaced by these regression tests.
