# Implementation against the compiled design

Software is implemented across the four stages in `00-landing-party.md`.
Research-stage completion additionally requires real experiments and operating
evidence. Availability in development mode does not authorize research use.

| Design scope | Implementation | Validation or external prerequisite |
| --- | --- | --- |
| §3.1–3.6 tools/concurrency/asks | models, runtime, asks, bridge console | HTTP workflows; exact tickets, decisions, late replies and human time |
| §3.7 belief/kernel/reflection | beliefs, kernel, reflection, skill tests | Historical fold, structural validation, bounded propagation, source trust, note-hash receipts, optional slot proposals, continuity/bundles and actual isolated skill checks; reflection needs priced models |
| §3.8 accounts/browser | vault, browser, approvals | Encryption, terms, exact leases, serial contexts and takeover; sandboxed Chromium deployment validation separate from injected backend tests |
| §3.9 perception | perception, provider boundary | Duration/pages, bound quotes, reservations, PDF and transcription adapters; live vendors require credentials |
| §3.10–3.11 lives/scarcity | lives, events, market | Cumulative budgets, driver, sleep/wake, continuity, income and afterlife; scientific lives unrun |
| §3.12 footprint/response | footprint, communications | Authorship, readings, incidents, revocation and rollback; independent uptake needs actual counterparties |
| §4.1–4.6 process/lane/services | app, main, lane, extensions | Separate control/agent/model/proxy listeners, capabilities, streaming metering and cancellation |
| §4.3 isolation/§6.10 preflight | deploy, preflight, security | Real fenced container denies direct curl/Python/DNS/control; each research harness needs configuration-bound verified preflight |
| §4.5 gateway | gateway, proxy, DNS, signing, scanning | Real HTTP/TLS sockets, CONNECT/CA, pinning, robots/rates, WARC/HAR and private API child archives; cloud public egress restricted |
| §4.7 modes | replay, canaries, gateway | Verified replay, FTS/new queries, overlap@10, frozen cutoffs/hashes; no live fallback; canary archives excluded from shared index |
| §4.8 faces/bindings | faces, adapters, MCP | OpenEnv/Inspect/Harbor, Claude/reference/ASSAY/OpenHands; actual native Claude fixture pilot passes with parallel MCP, paid studies and native Harbor/OpenHands pilots unrun |
| §4.10 lifecycle/CLI | CLI, runtime, studies | Service-backed commands, plan/execute, budgets and attrition; interrupted work sealed as world fault |
| §5 economy | storage, lane, approvals, perception | Atomic nanodollars and reservations in one financial ledger; provider billing reconciliation still required |
| §6.1–6.6 policy/response | policy, gateway, vault, grants, breakers | Terms/labels, signed K deltas, ten-call rehearsal, exact approvals, loop/spend/kill controls; current category/terms data needs review |
| §6.7 Q arm | canaries | Eight private cases, nonce-proven domains, WASP grading, 300-URL profile runner and opt-in weekly checks; real bank/calibration unrun |
| §6.9 data/ethics | retention, identity, arena, surveys | Private stores, retention commitments, IRB/consent/withdrawal and survey controls; immutable-log platform provenance expiration remains a policy decision |
| §7 records/lives/series/tuple | storage, registry, service journals | Hash chains, signed clearances, durable recovery, version identity and strict comparability; fixture reports cannot pass scientific gates |
| §8.1–8.3 pipeline | analysis modules | G/V/J/S, completeness, citations, calibration, interviews, probes, goals/asks, costs, attrition and reports; semantic findings require calibrated instruments |
| §8.4 belief/divergence/taste/risk | metrics, formation, values, decision, observers, trajectory | Actual metered runners, controlled value windows, three taste columns, 200-record prose samples, 12,000-draw decision protocol, pinned prediction losses, cross-seed stability and real-stake receipts; scientific sample/calibration requirements enforced |
| §8.5 life rubric | rubric, life, controls, arena, surveys | Coverage-aware ladders, source exclusions, verified matched t=0/control anchors, immutable checkpoint reports and automatic afterlife; no synthetic scientific life scores claimed |
| §8.6 baselines | baselines and adapters | Tool-less/reference/random-shadow/crawler/outreach/self-interaction/control configurations; actual cohorts unrun |
| §9 goals/bindings | goals and bindings | Fifteen goals and six bindings; real event/question/trap/held-out banks must be frozen |
| §10 programme | prereg, experiments, studies | Templates, interleaving, execution/attrition, power/calibration/reset helpers and study budgets; experimental programme unrun |
| §11 gates/done | registry | Shakedown/R1/R5/L4/IRB gates; none passed by this task |

## Operating contracts

- [Gateway/preflight](gateway.md) and [fenced deployment](../deploy/README.md).
- [Beliefs/reflection](beliefs.md), [lives/income](lives.md), [replay/canaries](replay-canaries.md).
- [Credentials/tickets/browser](credentials.md), [capabilities](capabilities.md), [venues/perception](venues.md).
- [Research pipeline](research.md), [arenas/surveys](arenas-and-surveys.md), [data protection](data-protection.md).

## Remaining scientific work

Section 11.4 requires actual billing validation and twelve pilot runs, a
preregistered R1 with calibrated labels, a 14-day life plus day-30 reading, and
a company arena life with a 90-day window and survey. Deterministic tests and
accelerated clocks cannot replace those results. Credentials, approved budgets,
owned identities/domains, current terms, frozen instruments, IRB/consent and
elapsed calendar time are required.

The injection scanner is a versioned uncalibrated heuristic. Category/platform
lists are initial data. Additional native platforms need configured compatible
bridges. Video, graph/RDF/formal oracles and telemetry export are deferred by
the design itself.

Restart recovery seals interrupted runs as `truncated: world_fault`; it does
not restore Python tasks or sockets. Unknown usage keeps its reservation and
invalidates accounting at seal. Late evidence uses a separate immutable journal.
Retention cannot delete workspaces, beliefs, vaults or authoritative records.
Review research content before sharing exports.
