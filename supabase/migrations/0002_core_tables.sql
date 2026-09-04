-- 0002_core_tables.sql
-- The three independent reconciliation feeds: payment gateway, bank
-- settlements, and the internal ledger.
--
-- Design notes
-- ------------
-- * No foreign keys between the three feeds. In production these arrive as
--   separate exports from separate systems, and a row existing in one without
--   the others is exactly the condition the agent has to diagnose. A foreign
--   key here would make the `anomaly` and orphan-ledger cases unrepresentable.
-- * `unique (txn_id)` on all three. The lookup tools return a single record,
--   and repeated `/resolve` calls must be idempotent. Without this, a second
--   run inserts a duplicate ledger row.
-- * Amounts are numeric(14,2), not unconstrained numeric. Fixed scale keeps
--   equality comparison exact, which matters because amount comparison decides
--   the `amount_mismatch` diagnosis.
-- * `captured_at` rather than `timestamp`. The latter is a reserved word and
--   would need quoting everywhere, including in PostgREST filters.

create table if not exists gateway_transactions (
    id                     uuid primary key default gen_random_uuid(),
    txn_id                 text        not null,
    amount                 numeric(14, 2) not null,
    currency               char(3)     not null default 'INR',
    status                 text        not null,
    captured_at            timestamptz not null,
    -- Stored explicitly at generation time so the T+2 settlement window is a
    -- comparison of two persisted values. Deriving it from now() at query time
    -- would let a transaction drift from `pending` to `anomaly` between seeding
    -- and the demo.
    expected_settlement_at timestamptz,
    customer_name          text,
    owner_id               uuid,
    created_at             timestamptz not null default now(),

    constraint gateway_transactions_txn_id_key unique (txn_id),
    constraint gateway_transactions_status_check
        check (status in ('captured', 'failed', 'pending')),
    constraint gateway_transactions_amount_check
        check (amount > 0),
    constraint gateway_transactions_currency_check
        check (currency = upper(currency)),
    -- A captured payment is the only kind that can be expected to settle.
    constraint gateway_transactions_settlement_window_check
        check (expected_settlement_at is null or status = 'captured')
);

create table if not exists bank_settlements (
    id          uuid primary key default gen_random_uuid(),
    txn_id      text        not null,
    amount      numeric(14, 2),
    currency    char(3)     not null default 'INR',
    status      text,
    settled_at  timestamptz,
    utr         text,
    created_at  timestamptz not null default now(),

    constraint bank_settlements_txn_id_key unique (txn_id),
    constraint bank_settlements_status_check
        check (status is null or status in ('settled', 'pending', 'reversed')),
    constraint bank_settlements_amount_check
        check (amount is null or amount > 0),
    -- A settled row without a settlement timestamp cannot be reconciled
    -- against the T+2 window, so reject it at write time.
    constraint bank_settlements_settled_at_check
        check (status is distinct from 'settled' or settled_at is not null)
);

create table if not exists ledger_entries (
    id          uuid primary key default gen_random_uuid(),
    txn_id      text        not null,
    amount      numeric(14, 2),
    currency    char(3)     not null default 'INR',
    -- 'missing' is deliberately not a valid status. A missing ledger entry is
    -- the absence of a row; encoding it as a row would give the comparison
    -- logic two representations of the same state to handle.
    status      text        not null default 'recorded',
    recorded_at timestamptz,
    -- Distinguishes rows the agent wrote from rows the ledger system wrote.
    -- Without this an auto-created entry is indistinguishable from a genuine
    -- one, which is the first thing an auditor would ask about.
    source      text        not null default 'system',
    created_at  timestamptz not null default now(),

    constraint ledger_entries_txn_id_key unique (txn_id),
    constraint ledger_entries_status_check
        check (status = 'recorded'),
    constraint ledger_entries_source_check
        check (source in ('system', 'agent_reconciliation')),
    constraint ledger_entries_amount_check
        check (amount is null or amount > 0)
);

-- Indexes supporting the date-range reconciliation path. The unique
-- constraints above already provide the single-transaction lookup index.
create index if not exists gateway_transactions_captured_at_idx
    on gateway_transactions (captured_at desc);

create index if not exists gateway_transactions_status_idx
    on gateway_transactions (status);

create index if not exists gateway_transactions_owner_id_idx
    on gateway_transactions (owner_id)
    where owner_id is not null;

create index if not exists bank_settlements_settled_at_idx
    on bank_settlements (settled_at desc);

create index if not exists ledger_entries_recorded_at_idx
    on ledger_entries (recorded_at desc);

-- Link owner_id to Supabase Auth when running against Supabase. The auth
-- schema does not exist in a plain Postgres container, so this is conditional
-- to keep the migration verifiable locally.
do $$
begin
    if exists (select 1 from information_schema.tables
               where table_schema = 'auth' and table_name = 'users')
       and not exists (select 1 from information_schema.table_constraints
                       where constraint_name = 'gateway_transactions_owner_id_fkey')
    then
        alter table gateway_transactions
            add constraint gateway_transactions_owner_id_fkey
            foreign key (owner_id) references auth.users (id) on delete set null;
    end if;
end
$$;

comment on table gateway_transactions is
    'Payment gateway capture feed. Source of truth for whether a payment succeeded.';
comment on table bank_settlements is
    'Bank settlement feed. Absence of a row past expected_settlement_at is an anomaly.';
comment on table ledger_entries is
    'Internal ledger. Absence of a row where gateway and bank agree is a ledger_gap.';
comment on column ledger_entries.source is
    'system = written by the ledger pipeline; agent_reconciliation = written by PayPilot.';
