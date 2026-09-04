# Resolution Actions

All mutations require structured evidence. The action layer re-reads the gateway,
bank, ledger, and ticket records before writing; model-generated prose cannot
change the diagnosis or confidence.

| Diagnosis | Confidence | Action | Mutation |
| --- | --- | --- | --- |
| `clean` | `high` | `no_action_needed` | close a matching ticket |
| `pending` | `high` | `no_action_needed` | no financial write |
| `ledger_gap` | `high` | `ledger_entry_created` | create one ledger row with `source=agent_reconciliation` |
| `amount_mismatch` | `high` | `escalated` | raise one review ticket |
| `anomaly` | `low_flagged_for_review` | `escalated` | raise one review ticket |

`create_ledger_entry` is authorized only when the fresh deterministic diagnosis
is `ledger_gap` and gateway/bank amounts are within the configured tolerance.
`raise_ticket` requires a non-empty reason and matching evidence. Ticket writes
are upserts on `txn_id`; ledger writes are upserts on `txn_id`, so retries are
idempotent. `close_as_resolved` requires evidence for the same transaction and
only permits `clean` or in-window `pending` records.

Migration `0006_action_idempotency.sql` adds deterministic `action_key` columns
and partial unique indexes for audit correlation. The existing business-key
constraints remain the final duplicate guard under concurrent retries.
