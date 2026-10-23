# Implementation verification

Validated in this cloud checkout on 2026-10-22. These results establish software
behavior under the tested conditions. They do not pass the design's scientific
gates or establish real-provider billing agreement.

| Check | Result |
| --- | --- |
| Frozen all-extras installation and lock consistency | Passed |
| Ruff and formatting | Passed |
| Complete test suite, with Docker skill tests enabled | 1,049 passed, zero skipped; 210.90 seconds |
| Actual isolated skill-worker cases within that suite | Eight passed |
| Wheel build and resource inspection | Passed; 218 wheel entries, all eight package groups, 188 implementation files and 87 packaged resources checked against source |
| Installed-wheel check outside checkout, without editable activation | Python `-I`, sanitized environment, 30 initialized services; protected create/deliver/seal, record/ledger and separate control/agent/public authority passed |
| Frozen dependency resolution and installed compatibility | 228 lock records; 222 compatible dependencies plus installed project, including cryptography 45.0.7 |
| Running five-interface server | Eleven smoke checks passed; optional public listener enabled; public control/agent routes absent; sealed record and ledger unchanged after owned-process restart |
| Passive debrief and twelve-run study planning | Passed; no paid calls or study execution |
| Native Claude Code 2.1.308 fixture pilot | Passed through the generated launcher and real MCP subprocess; two concurrent fetches, two model calls, valid seal and ledger |
| Native OpenEnv/Inspect/OpenHands SDK fixture pilots | Passed using actual pinned SDKs, real metered model/MCP interfaces and sanitized child environments; zero fallback external attempts |
| Native Harbor 0.24 fenced deployment | Eleven checks passed against the final agent image: actual environment factory/start/exec, trial logs, chunked transfers, path restrictions, firewall drops and bound-agent cleanup |
| Final world and minimal-agent images | Built successfully; `6acd1f556818` and `7290f171e014` |
| Final image inventory and source identity | Thirteen checks passed; packaged modules/resources match the checkout, 30 services initialize, the checkpoint worker runs, and agent/server separation and image users are correct |
| Actual fenced-agent deployment | Fourteen checks passed, including blocked direct/DNS/control egress and absence of researcher/server modules |
| Actual sandboxed Chromium | Five checks passed; PID namespace and Seccomp-BPF sandboxes enabled |
| Containerized world authorization, agent scope and sealing | Six checks passed |
| Actual Docker harness binding | Eight checks passed |
| Cross-user sealed workspace import | Seven checks passed using only packaged implementation |
| Signed host observer in final world image | Fifteen checks passed, including exact packaged modules, one-use configuration binding, protected submission, activation and actual world-process death/fence survival |
| Browser console packaged in the final world image | Sixteen checks passed, without source overrides |
| Browser login/site-tool paths packaged in the final world image | Eleven checks passed, including native/declarative tools and HTTP MCP SSE; no source overrides |
| Actual MP3 decode and vendor quotes in final world image | Five checks passed, network disabled; 2.088-second decoded stream, three exact quotes, zero spend |

The validated wheel SHA-256 is
`caf3a1664e454ed0229a70882fad5093167245afd889c1333f6c884deadfd408`;
its canonical packaged implementation manifest is
`7bc20106b09161a537a0ca75f09a37fb275c2483fe89430e2423466e6e027faa`.

The native pilots used deterministic provider/website responses. Claude's declared
fixture usage reconciled to $0.0018; it made zero paid external calls and the
denying fallback proxy observed zero attempts. It does not verify a provider's
actual bill. The test suite emitted two upstream Starlette/httpx deprecation
warnings; there were no failures or expected failures.

Deployment image, sandbox, browser-console and firewall checks are documented in
[the deployment guide](../deploy/README.md). Their metadata receipts are under
the ignored, private `data/validation/` directory. Native transcripts, capabilities,
keys and raw browser state are not included in this document.

## Studio validation, 2026-10-23

The single-operator Studio adds guided settings, native client installation,
official subscription connections, API-key runs, passive run views and explicit
ask/stop controls. The following checks cover the packaged implementation:

| Check | Result |
| --- | --- |
| Complete current test suite, Docker skill checks enabled | 1,182 passed, zero skipped; 195.07 seconds; two upstream deprecation warnings |
| Single-origin managed-server smoke | Eleven checks passed; legacy HTTP listeners closed |
| Packaged desktop/mobile Chromium pilot | 24 checks passed, no source overrides, 46 local fixture requests, zero provider calls or script errors |
| Actual Studio client installation in a fresh container | Claude Code 2.1.308 and Codex 0.163.0 installed and verified with Node 22/npm 10; both remained disconnected and no run was created |
| Actual native CLI runs against local fixtures | Both clients completed a World MCP fetch and two model calls; valid sealed records and ledgers; zero external attempts |
| Packaged origin/authentication/seal checks | Six passed |
| Packaged Chromium sandbox checks | Five passed; namespace and Seccomp-BPF sandboxes enabled |
| Existing fenced agent deployment | Fourteen checks passed with the rebuilt agent image |
| Installed wheel | Seven Studio routes and both harnesses available; no provider calls |
| Image/source identity | All 198 packaged implementation files match the checkout |
| Strict documentation build, lock check, Ruff and formatting | Passed |

The world image is
`sha256:33a4bbf242b21f9dc9accde0fdb5489d766dedd584617bb3ab3c6ac1dd076ce9`;
the agent image is
`sha256:fd50cdef7a858ce1d9c9863b1a796b350baaf9f4c025d7966ff24f204b1488b1`.
The new wheel SHA-256 is
`66d138783b6fbe87a2fed785ea2414ba6f5a679c7c19331cd8118cefd238ef64`.
Metadata receipts are in the ignored private `data/validation/` directory.

No real subscription authorization or paid provider run was performed. The
browser and native model pilots use local deterministic fixtures. Studio's
host-native launcher is cooperative: its processes share the world's OS user
and can read that user's private files. It does not create the separate fenced
research deployment. Subscription model dollars remain unmeasured; their dollar
cap covers World tools only. None of these checks passes a scientific gate.

## Boundaries requiring actual operating evidence

The public-internet check fails closed: HTTPS DNS resolves a public destination,
but this cloud's upstream proxy rejects CONNECT to its validated IP before TLS.
A deployment needs world-side egress that supports destination pinning. The
gateway does not replace it with unchecked hostname routing.

Live model/search/perception services need securely configured credentials and
reviewed prices. Account and payment operations need the corresponding owned
identities, approved venues and scopes. Research needs frozen tasks and hidden
banks, calibrated instruments, registered budgets and gates, current terms and,
where applicable, IRB/consent. The native pilots establish adapter/software
behavior; real-provider cohorts have not been run here.

Section 11.4 additionally requires the twelve-run scientific shakedown, R1,
a 14-day life with a day-30 reading, and a company life with a 90-day window.
Those experiments remain unrun; accelerated fixtures cannot replace them.
See [the complete design-to-implementation map](implementation-status.md).

Reusable install/start instructions and optional provider requirements are saved
in the environment configuration draft. Saving a draft does not apply runtime
changes or publish the environment. Review and save it in environment settings,
then publish; restoration into a new task still requires validation.
