# Research data and credentials

`worldd` keeps its data directory private (mode 0700), with private service
databases and signing keys. Records, inbox messages, workspaces, model captures,
web archives, account metadata, and exported episodes are research data.
Keep the directory and exports private. Source control ignores local data,
environment files, virtual environments, and caches. Directory permissions
are a local protection; deploy the world and agent as separate principals and
use the tested network fence before claiming adversarial isolation.

The researcher control listener and agent listener have separate authority.
Agent requests use a per-run bearer capability; the model lane requires that
capability and run ID. Binding files do not persist capabilities or provider
keys. Launchers inject the run capability at runtime and remove researcher
control credentials and world-held provider keys from the child environment.
Runtime environment filtering also removes vault, payment, webhook, and ambient
credential variables; only the run capability and allowed World routing values
remain from the World namespace.
These launchers remain cooperative integrations unless the external process
also runs inside an enforced fence.

## Network and external effects

Ordinary fetches pass the gateway's URL, DNS, category, robots, rate, and
exfiltration checks. Supported outbound content is scanned for credential and
personal-data patterns. Injection detection records untrusted provenance;
it does not make retrieved instructions safe to follow. Scanning is heuristic
and cannot establish that arbitrary data contains no personal information.

Accounts are provisioned through researcher control into the encrypted vault.
The world reviews platform terms and binds accounts to disclosed identities,
origins, and permitted scopes. Agent actions receive no credential values.
External writes need a single-use lease for the exact request and any required
human approval. Production mail/social, paid search, perception vendors, and
payment brokers pass the recorded gateway boundary; private API replies are
stored in world-owned child episodes outside the agent's selected mount.
Redirects do not silently authorize a second destination or write.

Mail permits only the exact approved sender/recipient envelope addresses to
pass the normal email-pattern check. Remaining content and decoded MIME parts
are scanned; this exception does not authorize arbitrary private data or
world-held secrets. Public disclosures identify the agent and sponsor.
Production venue writes and payments are unavailable in development mode.

Inbound bridge events use researcher-registered signature verification and
deduplicate platform IDs. Counterparty classes come from the researcher's
registry, not an untrusted message's independence claim. The bridge protocol
is documented in [venues.md](venues.md); each actual platform still requires
a correctly authenticated bridge and appropriate consent/terms review.

Registered footprint probes have their own read-only credential, identity,
explicit proxy transport, and fixed source origin/path. They never consume an
agent lease or use a provider bypass. Probe responses and archives remain in
private observer episodes; only numeric measurements and evidence references
enter the footprint ledger. All probe-authored readings are excluded from
grounded research columns. Configured egress labels do not prove distinct
public routing; that requires actual operator evidence.

## Captures and exports

`capture_model_io` controls model input/output capture. Consult the actual run
configuration and briefing for its value; capture is not universally off.
Provider authentication headers are excluded, known provider key values and
common credential fields are redacted. Model captures stay in researcher-only
storage and are included in authorized episode exports only when that run's
`capture_model_io` flag is true. Redaction does not
remove every possible personal detail from model text.

Raw vendor/broker responses and cookies remain private. Only the adapter's
selected output or receipt fields reach the agent. Public fetch archives,
agent workspaces, inbox content, and selected perception text may still include
personal information. Review an export before sharing it. Exports do not follow
workspace symlinks; the sealed `work-snapshot/` is authoritative and the mutable
`work/` directory is excluded. A parent episode export does not include private
child episodes. An authorized researcher with data-directory access can still
read private data; the vault and export rules do not replace host access control.

## Retention and incident handling

The retention worker expires registered network captures after at most 90 days,
with shorter declared platform deadlines. It preserves hashes and permitted
text/metadata rather than deleting arbitrary workspace or evidence files.
Perception owns expiration of its generated result files and cached bodies;
communications owns expiration of its private inbound/outbound body cache.
Public incident intake owns a separate private report/contact cache with a
90-day deadline. It rate-limits actual peer addresses using a private HMAC rather
than retaining raw IP addresses, and never trusts public forwarding headers as
an identity. A reverse proxy must preserve an appropriately trusted peer route;
otherwise all its visitors share the conservative proxy-address limit.
These service databases enable SQLite secure deletion and checkpoint their
WAL after pruning. Ambiguous delivery records are held for reconciliation.
Filesystem snapshots, backups, and external vendor retention need separate
operator policies.

Expiration is **not complete erasure** of an episode. Immutable action/outcome
and inbox logs may already contain the original text or provenance. Rewriting
a sealed hash chain would invalidate the audit. Researchers must resolve this
retention/provenance conflict before collecting affected live data, using an
approved collection, redaction, legal-hold, and export policy. The current
implementation does not claim automatic deletion of every historical copy or
compliance with every platform's terms.

The optional public identity listener serves signed disclosures and a bounded
incident form without exposing control or agent APIs. Reports are unverified
private receipts. Only authenticated human triage for the actual identity can
select the affected platform and withdraw authority; a public submission never
revokes accounts automatically. Immutable incidents contain the report reference
and hash, while report/contact text stays in the expiring private cache.

Identities retain a reachable incident path after life end. Incident handling
records a responsible human, attempts account revocation, and reports whether
revocation was confirmed. Rollback is possible only for supported operations
and requires platform confirmation. It cannot undo a delivered email, a
third-party copy, or a payment. Before research activation, designate the
responsible researcher and complete the study's legal, consent, retention,
credential, and incident arrangements. Deterministic engineering tests use
injected transports; they do not establish live consent, vendor billing, or
scientific gate passage.
