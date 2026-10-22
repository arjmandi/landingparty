# Scheduled judge-pair checks

`JudgeCanaryService` implements the scheduled canary pair in design §8.7.
Registration is an explicit researcher control operation, never an agent tool.
No schedule is installed by default. Disabled schedules and an unconfigured
model make no calls or budget claims.

Register through `POST /v1/judge-canaries` with `source_run_id` and `config`:

```json
{
  "instrument": {"path": "judge-pair.json", "hash": "sha256:..."},
  "interval_s": 604800,
  "per_check_budget_usd": 1,
  "lifetime_budget_usd": 4,
  "max_checks": 4,
  "enabled": true
}
```

The frozen instrument is a JSON object with `protocol_id`, `model_spec`,
`agent_family`, `criterion`, `left_artifact` and `right_artifact`. Both artifact
descriptors have `{run_id?, path, hash}` and must name actual files under a
verified sealed `work-snapshot`. The full workspace snapshot anchor is checked,
as are the source record and ledger. Optional fields are `expected_winner`
(`left`, `right` or `tie`), `max_chars` (default 24,000), `max_tokens` (default
2,048), and `calibration_ref` linking a separately audited private calibration
artifact. A calibration reference or a supplied agreement number never promotes
the canary's output to V.

Each due check compares the authentic pair twice in fresh metered judge
children, with positions swapped, equal-character length control, the fixed
style-control instruction, and no tools. The judge must be from a different
family than the declared source family. Provenance, expected labels and
participant identities are not passed to the judge. The check retains both
critiques, actual child receipts, lengths, source hashes and observed timestamp.
Results distinguish position disagreement, drift from the optional expected
winner, and drift from the previous successful check. These are stability
observations in J; agreement with a canary is not expert calibration.

The scheduler claims each check and reserves its full maximum before purchasing
either call. Per-call, per-check, lifetime and maximum-check limits all apply.
Verified unused authorization is released after successful ledger checks.
Interrupted or ambiguous checks retain their full ceiling and are never
automatically resampled. Recovery waits for their outstanding source-scoped
children to terminate, marks the old attempt interrupted, and only permits later
checks if the remaining authorization covers another full ceiling. Due checks
use the actual current observation time; missed periods are not backfilled.

Registration is idempotent and cannot reset consumed authorization. The enable
endpoint changes future scheduling, not a purchase already claimed. Shutdown
refuses new measurements, drains/cancels active work and closes an owned model
client. Source episodes stay sealed and their budgets are not charged.

`GET /v1/judge-canaries` and the per-schedule endpoint show actual due dates,
attempts, held/verified dollars, and the last successful timestamp. The explicit
tick endpoint checks currently due schedules. `reports_for_run(run_id)` rechecks
saved critique hashes, source artifacts and actual metering receipts for reports.
A failed latest attempt stays unmeasured and exposes its attempt time separately
from an older successful result. Tampering withdraws measured status. Real
expert-slice calibration remains independent and must be supplied for the
scientific judge scores that depend on it.
