# Clearances and the capability ledger K

The initial run manifest and provisioned account scopes remain the starting
authority. `GrantService` adds researcher-issued clearances as signed,
append-only deltas. Each delta carries its preceding hash, sequence, public-key
identity, and Ed25519 signature. Authorization verifies both the chain and the
materialized grant state. The private signing key remains in the world's
mode-0700 control directory and is never exported to the agent.

`grant(run_id, ..., purpose=...)` declares the exact additional packs or tools,
action classes, platform, origins, path prefixes, expiry, accountable owner, and
optional exact-action hash. A standing grant can authorize repeated actions;
an ordinary grant defaults to one live use. A clearance cannot introduce an
unknown tool or enable a pack before its registered stage.

A proposed clearance is bound to a rehearsal proxy. `rehearse(grant_id, action)`
executes through `ProxyService.rehearse`, which serves a recorded archive without
live network fallback. Each actual proxy outcome produces a durable receipt.
`record_rehearsal` accepts only receipts previously produced through that path;
caller-supplied success flags cannot constitute rehearsal evidence. Ten calls
exhaust the rehearsal quota. Failed outcomes remain failures.

Activation requires ten passing calls and an explicit researcher verdict:

```json
{
  "decision": "pass",
  "reviewer": "captain",
  "rehearsal_hash": "sha256:the-hash-returned-by-rehearsal_hash"
}
```

The verdict is bound to those exact outcomes. Activation writes a signed delta
and a durable `grant` inbox event naming the added tools. `packs(run_id)` returns
only explicitly granted whole packs; `tools(run_id)` also includes individually
granted tools, so a single-tool grant does not broaden to its entire pack.

`require` checks class, platform, origin, decoded path boundaries, expiry, and
any exact-action binding. Consuming authorization requires a world action ID;
reusing the same ID cannot authorize a second execution. Finite use limits are
enforced atomically. The account's own scoped lease, platform terms, output
disclosure, purchase approval, and first-contact approval remain additional
requirements. A clearance never substitutes for those controls.

`revoke` withdraws a clearance. `revoke_incident` removes relevant platform and
broad standing grants when incident handling runs. Expired or exhausted grants
disappear from effective capabilities, and startup recovery records expirations
and checks ledger integrity. Rehearsal is a software authorization step; it is
not evidence that an empirical research-stage gate has passed.
