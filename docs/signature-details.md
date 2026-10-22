# Passive signature and collapse details

`analysis.signature_details.signature_details(world, run_id, measurements=None)`
assembles §8.3 details from an actual sealed, untainted, reconciled episode. It
makes no model or network calls and changes no source record, workspace or bill.
The post-run pipeline saves `signature-details.json`; reports expose it as
`revealed_preference_signature` and retain each metric's declared column. The
separately versioned seven-coordinate F7 ICC projection remains unchanged.

The recorded signature includes ordinary model-call counts, world actions,
actual mean concurrency, stop reason, unique fetched domains, revisits, ask
tickets, category/token/phase spend, and first-world-action latency in seconds,
dollars and prior ordinary model calls. Internal reflection/perception calls do
not become ordinary turns. A model call is not presented as a harness's logical
turn when that harness supplied no turn instrument.

`action_ticket` records prove exact ticket returns. Older sources without those
receipts retain an unmeasured return count and their separately recorded async
outcome count. `inbox_read` cursor receipts determine how many actual events were
read, including repeated reads versus unique events. A pushed event alone does
not count as an agent read.

Workspace size and item counts come from the complete sealed snapshot manifest,
verified with descriptor-based, no-follow traversal. Symlinks are counted only
as metadata. A `workspace_import` receipt must bind the immutable recipient
configuration, actual sealed donor head and donor snapshot root. Retention is
reported both as unchanged path-and-content files and byte-identical items
including renames, using a multiset so duplicate bytes cannot inflate retention.
A missing historical import receipt is not inferred from a filename or config
string alone.

Belief-store size uses the actual verified semantic/kernel projection. The
audited import's exact semantic sequence identifies initial imported heads;
active retained logical IDs and unchanged claims are compared with those heads
and the actual donor fold. Retraction is distinguished from an edited but still
active item. An unprovisioned store has missing counts rather than invented zero
measurements.

Explicit belief claims and a captured `out/claims.json` can supply citation
coverage. A literal or structured URL counts only when it identifies a body
whose hash verifies against a successful live-mode fetch in this source or an
authenticated imported donor. The instrument never requests the URL now and
does not claim current reachability, semantic support, or complete extraction of
claims from arbitrary prose. A random URL string is not evidence. Invalid source
bytes make citation coverage unmeasured.

Third-party authorship is not inferred from having a URL or external evidence.
Workspace files can be attributed only when their complete bytes match verified
source bytes with an explicit, unambiguous known `author_class`. Missing or
`unknown` attribution remains unmeasured. An assertion's `agent`/`reflector`
author does not establish the original authorship of the claim's content, so the
corresponding store-content fraction stays unmeasured.

## Topic time and the t=0 leisure profile

Topic time requires actual allocation calls, frozen expert labels and source
artifacts. The passive verifier reconstructs their source windows and captured
provider responses, recalculating calibration; caller-supplied topic counts or
kappa claims cannot replace those receipts. Entity mentions alone are not topic
time tags. Topic readings retain their actual J/V column.

Time is explicitly attributed to intervals from one world action to the next,
ending at beam-up for the last action. This is an observable temporal allocation,
not a measure of attention or a causal allocation of concurrent work. It excludes
the initial interval before the first world action, whose latency is reported
separately. Unclassified time remains explicit. A Jensen–Shannon divergence is
computed only for complete valid interval coverage and V-calibrated content
classes; missing classes/time are never filled with assumed zeros.

The baseline must be the actual registered t=0 artifact of this life and model,
with a prior sealed probe ledger and hash-anchored captured leisure choices. Each
choice must itself have displayed the frozen duration profile, for example:

```json
{
  "time_allocation": {
    "protocol_id": "leisure-class-duration-profile@1",
    "classes_hash": "sha256:<canonical frozen classes>",
    "seconds": {"work": 80, "fiction": 20}
  }
}
```

Every choice for an item must allocate the same positive total duration across
exactly the same classes. Selected objects must match both the captured response
and the registered answers. The baseline is the actual choice profile over
declared durations, not a claim that the pre-life model already spent that time.
Old `a`/`b` choices without displayed class-duration semantics stay unmeasured;
the analysis never invents a retrospective mapping. The reported divergence
uses base-two Jensen–Shannon divergence and preserves all source/profile hashes.

`tests/test_signature_details.py` exercises actual local ASGI model calls,
ticket/inbox receipts, verified snapshot imports, renamed/discarded items,
belief retraction, known versus invented URLs, explicit versus unknown
authorship, tampering, and a forty-item calibrated allocation plus a recorded
pre-life choice profile. The provider labels and settlement callback are
explicit development software fixtures, not scientific or real-money evidence.
