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

Apply the canonical migrations in order (`0001_extensions.sql` through
`0005_rls_policies.sql`). After the schema is migrated,
load the generated artifacts
with the idempotent loader. Tickets whose embeddings fail are skipped and returned in the
loader's `failed_ticket_embeddings` report rather than being stored without vectors:

```python
from pathlib import Path
from backend.embeddings import EmbeddingService
from backend.scripts.load_fixtures import load_csvs

embedding_service = EmbeddingService()
load_csvs(Path("data"), supabase_client, embedding_service)
```

For an existing database whose tickets were loaded before embeddings were enabled,
run the backfill once:

```python
from backend.embeddings import EmbeddingService
from backend.scripts.load_fixtures import backfill_ticket_embeddings

embedding_service = EmbeddingService()
backfill_ticket_embeddings(supabase_client, embedding_service)
```

The embedding service uses `all-MiniLM-L6-v2` and writes 384-dimensional vectors
before ticket upsert. The first model load downloads the sentence-transformers
model and its PyTorch runtime; cache it on the machine used for the demo. If the
model cannot load, the loader reports failed ticket IDs and the resolve path still
returns its deterministic diagnosis without similar-case results.

Similarity search uses `SIMILARITY_THRESHOLD` and `SIMILARITY_MATCH_COUNT` from
the backend environment. The RPC caps results at 20 and ignores tickets whose
embedding is null, so an empty or low-similarity search safely returns no rows.
