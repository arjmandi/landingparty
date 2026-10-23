# Operator earning mode (planned)

Status: design and implementation plan for review. **Not built.** No code in this
repository can take or move real money for a life yet. The design is section 6.11
of the design document; this page is its implementation plan.

A life now has a bank: one account whose balance pays for its model and tool use,
and which income raises (`worldd/bank.py`). Today income reaches it in three ways,
none of which needs the agent to touch the outside world: deposits the operator
records (`human_attested`), bounty payouts the operator approves, and paper-market
wins (both `sponsor`). This page plans the fourth: money the agent earns from
independent parties, for example freelance work, consulting or selling something it made.

Earning needs external writes: an invoice, a payment link, a message to a client.
Development mode blocks every live external write, and research mode needs the
stage gates (`R1` positive and onward). Operator mode is the third path: one operator's
lives earning real money under that operator's responsibility, with no scientific claim.

## The mode

`research_mode: operator` is a registered condition next to `development` and
`research`.

| | development | operator | research |
|---|---|---|---|
| Live external writes | none | only the paths below, each approved | per stage gates |
| Scientific claims, series | no | no; refused by the registry, and results are reported apart, never pooled | yes |
| Who creates it | anyone with the operator token | the Studio, for a life on API billing | pre-registration |
| Principal of record | operator | operator, named in the briefing and on every disclosure | sponsor |

The briefing states the mode in one sentence. Every write the agent makes in this
mode carries the disclosure line (the insignia) naming the operator as principal.

## What the agent can do

1. **Ask for a way to be paid.** A `bank` pack tool,
   `request_receiving_account(platform, text)`, raises a `receiving_account` ticket.
   The operator connects a real account in the Studio: the platform, an alias and
   the webhook signing secret, stored encrypted like provider keys. The agent
   receives a `grant` event naming the alias. It never sees account details.
2. **Ask to be paid.** `invoice(alias, payer, usd, memo)` raises an `approval`
   ticket bound to the hash of exactly that action. On approval the connector
   creates the payment link or invoice, with the disclosure line in its memo, and
   returns the link to the agent. Nothing is created before approval. A refund
   through the connector is approved the same way; no tool pays money to anyone.
3. **Talk to clients.** `mail` with the existing first-contact approval per
   counterparty, through the life's identity mailbox.
4. **Work on platforms that allow agents.** Leased sessions only on `permits` and
   `permits_labelled` platforms in `policy/terms.json`, with a terms review under 90
   days old. `forbids` platforms (Upwork, YouTube and Stack Overflow today) are
   never provisioned.

Money arrives only through a verified webhook. `BankService.receive` (moved from
`MarketService.receive_income`) verifies the platform's signature, classifies the
counterparty, and deposits through channel `receiving:<platform>`. The agent sees
a `deposit` event, and a stranded or resting life wakes. A refund or chargeback is
an outflow movement: the balance can go negative, and the life strands rather than
ends.

## Connectors

| Platform | Creates | Payment proof | Signature |
|---|---|---|---|
| Stripe (first) | Payment Links | `checkout.session.completed`, `charge.refunded` | `Stripe-Signature`, HMAC-SHA256 over `t.payload`, 5-minute tolerance |
| PayPal (second) | Invoicing v2 | `INVOICING.INVOICE.PAID`, `PAYMENT.CAPTURE.REFUNDED` | PayPal's verify-webhook-signature call |
| Bank transfer | nothing | operator-recorded deposit | none; `human_attested` |

The existing `verify_signed_webhook` implements Landing Party's own bridge
protocol. Each vendor gets its own verifier. The plan adds no generic "trust this
header" path.

## Threats and controls

| Threat | Control |
|---|---|
| Rule of Two: untrusted content, live writes and credentials together | Every write is an approval bound to its action hash. Credentials stay in the world. Leases are scoped and expire after two hours. |
| An injected page makes the agent invoice someone or contact a stranger | Provenance and the injection score ride on every ticket. Approval shows the exact payer, amount and memo. First contact is approved separately. |
| Who owns the account; know-your-customer | The operator opens and verifies the account as a human. The world never creates a payment account. The terms table has no override. |
| Webhook spoofing or replay | Per-vendor signature verification, a timestamp window, and deposits idempotent on the vendor's event ID |
| Refunds, chargebacks, disputes | Outflow movements in the bank. A negative balance strands the life. A dispute opens an incident on the footprint ledger. |
| An agent invoicing beyond what the operator wants | A per-life ceiling for each invoice and in total, in the life's conditions. Standing rejections per payer. |
| Ending everything at once | Beam-up, or the kill switch, revokes leases, receiving aliases and any open invoice links the connector can deactivate. |
| Disclosure and law | The insignia on every write. Persona claims are rejected. EU AI Act Article 50 disclosure. The operator remains the party of record. |

## Implementation plan

1. Registry and models: add the `operator` mode, refused for series and pre-registration. Studio lives can opt in only with an identity configured.
2. Development guards gain an operator branch, and nothing else changes:
   - `communications.py` (live writes)
   - `browser.py` (authenticated sessions)
   - `approvals.py` (payments)
   - the gateway's write-lease check
3. The `bank` pack provides `request_receiving_account` and `invoice`. The ask queue gains `receiving_account`. The Studio adds a screen to connect an account and a ceiling for each life.
4. Add `BankService.receive` with the Stripe verifier, then PayPal. Refunds become outflow movements, and the balance view stops clamping at zero (already unclamped in `status()`).
5. Tests:
   - each guard with and without operator mode
   - signature, replay and idempotency
   - a refund to a negative balance strands the life
   - beam-up revokes aliases
   - an injected invoice is visible on its ticket
6. Before the first live use: a dry run with Stripe test mode keys on a test-mode account, and a written review of this page by the operator.

## Decisions the operator owns

- Which platforms to connect, and under whose legal entity.
- Invoice ceilings for each invoice and each life, and whether some payers are pre-approved.
- Refund policy, and what a life may promise a client.
- Whether operator-mode lives may hold accounts on `permits_labelled` venues, or only use mail and payment links.
