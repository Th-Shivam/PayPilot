# Deterministic Fixtures

The development seeder generates 100 primary transactions with `random.Random(42)`:

| Path | Count | Gateway | Bank | Ledger | Expected behavior |
| --- | ---: | --- | --- | --- | --- |
| `clean` | 60 | captured | settled | recorded | resolved, no action |
| `ledger_gap` | 15 | captured | settled | missing | create ledger entry |
| `pending` | 10 | captured | pending | missing | no action inside T+2 |
| `anomaly` | 10 | captured | missing | missing | escalate after deadline |
| `amount_mismatch` | 5 | captured | settled | optional | escalate when outside tolerance |

Transaction IDs are stable, for example `txn-ledger-gap-001`. Every source CSV includes
`expected_settlement_at`, so classification does not depend on the date the seed was run.

Matching uses `transaction_id` as the primary key. Amounts may differ by at most `$0.01`;
timestamps are enforced with a five-minute source-event tolerance. A pending bank record is
safe only while the supplied reference time is at or before `expected_settlement_at`.

Generate artifacts with:

```bash
python -m backend.scripts.seed_fixtures --output-dir data
```

The command writes `gateway_records.csv`, `bank_settlements.csv`, `ledger_records.csv`,
`historical_tickets.csv` (20 resolved examples), and `edge_fixtures.csv` (duplicate and
already-resolved examples).
It is safe to rerun because generated records have stable IDs; database loading should use
upsert on the `transaction_id` unique key supplied by the schema migration.

Apply migrations in numeric order (`001_fixture_schema.sql`, `002_fixture_idempotency.sql`,
then `003_ticket_similarity.sql`). After the schema is migrated, load the generated artifacts
with the idempotent loader. Tickets whose embeddings fail are skipped and returned in the
loader's `failed_ticket_embeddings` report rather than being stored without vectors:

```python
from pathlib import Path
from backend.scripts.load_fixtures import load_csvs

load_csvs(Path("data"), supabase_client, embedding_service)
```
