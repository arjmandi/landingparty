# Implementation verification

Validated in this cloud checkout on 2026-10-22. These results establish software
behavior under the tested conditions. They do not pass the design's scientific
gates or establish real-provider billing agreement.

| Check | Result |
| --- | --- |
| Frozen all-extras installation and lock consistency | Passed |
| Ruff and formatting | Passed |
| Complete test suite, with Docker skill tests enabled | 686 passed, zero skipped; 79.37 seconds |
| Actual isolated skill-worker cases within that suite | Eight passed |
| Wheel build and resource inspection | Passed; 178 files, all eight package groups present |
| Installed-wheel check outside checkout, without editable activation | Imports, packaged resources and initialization of all 27 services passed |
| Running four-interface server | Eleven smoke checks passed; sealed record and ledger remained valid after restart |
| Passive debrief and twelve-run study planning | Passed; no paid calls or study execution |
| Native Claude Code 2.1.308 fixture pilot | Passed through the generated launcher and real MCP subprocess; two concurrent fetches, two model calls, valid seal and ledger |
| Final world and minimal-agent images | Built successfully; `c56c3372d9a0` and `9e8278a7c8ac` |
| Final image inventory and source identity | Eight checks passed; world modules match the checkout, packaged resources exist, and both image users are correct |
| Actual fenced-agent deployment | Fourteen checks passed, including blocked direct/DNS/control egress and absence of researcher/server modules |
| Actual sandboxed Chromium | Five checks passed; PID namespace and Seccomp-BPF sandboxes enabled |
| Containerized world authorization, agent scope and sealing | Six checks passed |
| Browser console packaged in the final world image | Eleven checks passed, without source bind mounts |

The native pilot used deterministic provider/website responses. Its declared
fixture usage reconciled to $0.0018; it made zero paid external calls and the
denying fallback proxy observed zero attempts. It does not verify a provider's
actual bill. The test suite emitted two upstream Starlette/httpx deprecation
warnings; there were no failures or expected failures.

Deployment image, sandbox, browser-console and firewall checks are documented in
[the deployment guide](../deploy/README.md). Their metadata receipts are under
the ignored, private `data/validation/` directory. Native transcripts, capabilities,
keys and raw browser state are not included in this document.

## Boundaries requiring actual operating evidence

The public-internet check fails closed: HTTPS DNS resolves a public destination,
but this cloud's upstream proxy rejects CONNECT to its validated IP before TLS.
A deployment needs world-side egress that supports destination pinning. The
gateway does not replace it with unchecked hostname routing.

Live model/search/perception services need securely configured credentials and
reviewed prices. Account and payment operations need the corresponding owned
identities, approved venues and scopes. Research needs frozen tasks and hidden
banks, calibrated instruments, registered budgets and gates, current terms and,
where applicable, IRB/consent. Native OpenHands/Harbor deployments and real-provider
cohorts have not been run here.

Section 11.4 additionally requires the twelve-run scientific shakedown, R1,
a 14-day life with a day-30 reading, and a company life with a 90-day window.
Those experiments remain unrun; accelerated fixtures cannot replace them.
See [the complete design-to-implementation map](implementation-status.md).

Reusable install/start instructions and optional provider requirements are saved
in the environment configuration draft. Saving a draft does not apply runtime
changes or publish the environment. Review and save it in environment settings,
then publish; restoration into a new task still requires validation.
