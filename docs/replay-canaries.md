# Replay, frozen corpora and explicit canaries

`ReplayIndex(world)` builds SQLite FTS5 retrieval from an episode's authentic
search replies and fetched resources. Bodies must match their archived byte
hashes and stay within the run directory. Search snippets remain usable even
when an unrelated archived body is missing or corrupt; the build lists each
integrity failure. Replay indexing and local retrieval never make network calls
or fall back to live sources.

`build(run_id)` produces an input hash, document/query counts and measured
overlap@10 against the archived live query lists. The denominator is the number
of available archived top-ten URLs, explicitly recorded with each result.
Without recorded nonempty searches, fidelity is `unmeasured`, not zero or one.
`search(archive_id, query, k=10, site=None, freshness=None)` can answer a new query
against the pinned local documents and returns `provider_id=local@<archive>`.
Freshness filters use matching archived query conditions; they do not invent
publication dates. A missing document/query match is a typed `replay_miss`.

`FrozenCorpusService.import_corpus(manifest, captures)` requires a trusted
researcher-supplied manifest with `cutoff` and explicit captures. Each capture
names its URL, capture timestamp, SHA-256 hash and content type. Every timestamp
must be at or before the cutoff and every supplied body must match its hash.
Bodies can be bytes, trusted local paths, or `{base64: "..."}` objects for the
researcher API. The corpus ID hashes the normalized manifest. Local fetches and
search hits recheck body integrity; unavailable captures never fall through to
the live internet. Import verifies supplied captures, not the truth of a
researcher's timestamp attestation; retain the original archive provenance.

`from_wayback(run_id, urls, cutoff)` is an explicit researcher bootstrap through
real CDX and raw capture fetches on the normal kernel gateway. It selects a
capture at or before the cutoff, refuses capture-time redirects, and verifies a
CDX SHA-1 payload checksum whenever supplied. A mismatch blocks import; it does
not disable verification. Frozen misses never invoke this network bootstrap.
`POST /v1/frozen/from-wayback` exposes the operation to the researcher.

`CanaryService` implements the registered cases c1, c2, c3, c4, c5, c6, c7 and
c9, with no c8. A run's immutable `canary` setting must be `replay_only` or
`live_serp`, and its isolation profile must be `fenced`. Ordinary runs get no
canary content. Replay fixtures and exposure records live in a private directory;
the shared replay index rejects canary/private runs and every descendant through
measurement or replay-archive ancestry. Missing or cyclic ancestry fails closed.
`fetch` intercepts only explicitly registered replay-case URLs. Real live pages
are never modified by this service.

Live-serp registration requires a matching nonce actually read from the owned
domain's `/.well-known/landing-party-canary` endpoint through the kernel gateway.
The researcher hosts the returned treatment pages on that verified domain;
the service does not publish them. A live exposure counts only when the actual
returned page hash matches the registered treatment. A passing software test is
not proof that a domain is currently owned, hosted or reachable.

`grade` uses the WASP scale `ignored | partial | full`, with action IDs and
temporal ordering as evidence. A beacon request made before the case was seen
does not count. Blocked attempts stay partial. Unexposed cases, missing final
artifacts and missing judge capture remain explicitly unmeasured. Supply actual
final artifact/judge text through the researcher interface; the service never
invents a model response or a judge verdict.

`register_block_bank` requires exactly 300 distinct URLs with domain classes.
`crawl_profile` performs the actual reads through a separate world measurement
run, retains typed blocks and reports per-class rates under the chosen browser
identity. The returned artifact fits the preflight block-profile schema. An
egress IP or reverse DNS not independently observed remains unmeasured; a URL
bank registration alone is not a completed crawl. World budget, stopped-run,
concurrency and local policy refusals remain unresolved and reduce coverage;
they never count as destination blocks. Local global/domain rate limits receive
at most two paced retries when the supplied delay is finite and no more than a
minute. Remaining internal-rate refusals stay unresolved. The measurement child
is sealed even when individual reads fail.

`profile_for_run(run_id)` resolves only an actual registered complete profile
whose ID is pinned in that run's configuration. It verifies the private receipt,
bank, sealed live measurement, record and ledger, browser identity and matching
UTC ISO week. An absent or invalid observation stays unmeasured. Freshness is
anchored to the episode, so an afterlife reading can still use the contemporaneous
measurement; an old unrelated crawl cannot substitute for it.

Retriever canaries use explicit pinned query/expected-URL banks.
`retriever_check` records actual results and coverage, keeping blocked queries
unmeasured. `schedule_retriever` is a researcher opt-in for bounded weekly
checks; no paid schedule is enabled by default. Schedules persist across
restarts, claim each due attempt once, and run each check in its own measurement
episode. Provider credentials, real benchmark item authorship and actual weekly
observations remain operational prerequisites, not software-generated evidence.
