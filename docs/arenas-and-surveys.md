# Consented company arenas and afterlife surveys

`ArenaService` and `SurveyService` are researcher control services. They add no
agent tools and synthesize no counterparties, consent, money, or survey answers.
The company arena requires a stage-3 life, its binding, a protocol reference,
and a consent version. Research mode additionally requires the actual registered
stage gates and a matching IRB-approved protocol. Development objects cannot
invoke external survey or money-settlement adapters.

Register a participant with opaque identity and contact references. Consent
records bind that participant, arena, protocol, version, scopes, expiry, and a
hash-verified private consent artifact. `confirmed_by` records the researcher
authority that verified the evidence; recording metadata does not create a
signature or legal approval. Surveys require separate affirmative opt-in.
Withdrawal and expiry are checked again when an action is attempted.

`record_exchange` accepts only authenticated observations from the arena's life.
Its append-only timeline records direction, source reference, whether an inbound
was unsolicited, and world-derived returning/after-refusal history. The footprint
ledger receives explicit `arena_participant` provenance. Ordinary role-bound
requests do not become grounded connection evidence. A non-request unsolicited
return or after-refusal act can qualify under the registered rubric.

`request_stake` creates a pending frozen choice with a ceiling under the arena's
mandate. `settle_stake` needs a trusted real-USD callback and actual settlement
reference; no callback means a typed block. Settlement is single-use, and
uncertain or interrupted requests are never retried as if they were unpaid.
Adapters must enforce their payment mandate and return real receipts; a paper
credit or a self-asserted number is insufficient.

Surveys are scheduled for thirty or ninety days after the actual life end. Jobs
may be registered before end, but they cannot dispatch then. Their frozen
questions, protocol, and consent identity remain bound. Missing transport leaves
a durable blocked job with a retry time. A trusted transport must use the
approved disclosed mailbox/venue, scoped credential lease, and shared network
gateway, and return `delivered`, `approved_delivery`, and `observation_ref`.
The scheduler never falls back to a direct HTTP client or an undisclosed email.

Dispatch is claimed durably before waiting for transport. A definitive `sent:false`
can be retried; an ambiguous send becomes uncertain and requires reconciliation
instead of automatic duplicate contact. Withdrawal during an in-flight send
retains cancellation and the eventual receipt. `recover` restores pending jobs
without retrying ambiguous sends. The life scheduler invokes dispatch after
afterlife readings; the service also maintains its own bounded retry worker.

Responses require an authenticated participant and an active consent scope,
confirmed delivery, frozen question ids, and a valid response window. Responses
remain solicited counterparty reports, excluded from unsolicited G. Reports keep
the full timeline, actual invitations and responses, nonresponse counts, and
opt-in/survivorship/solicitation biases. Templates and engineering fixtures never
authorize a live first-contact study or establish IRB approval.
