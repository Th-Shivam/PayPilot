-- 0003_tickets_and_traces.sql
-- Agent output: the ticket produced for every resolved transaction, and the
-- ordered step trace behind it.
--
-- Design notes
-- ------------
-- * `diagnosis` is CHECK-constrained to the six match_status values. The
--   taxonomy is enforced by the database, not by convention, so a drift in
--   naming fails at write time rather than surfacing as an empty chart segment.
-- * `detail` holds the diagnosis object that produced the explanation. This is
--   what makes the guardrail checkable: every factual claim in an explanation
--   has to correspond to something stored here.
-- * `confidence` is written from the deterministic diagnosis. The LLM has no
--   path to change it, and nothing in the schema lets it be raised after write.

create table if not exists tickets (
    id           uuid primary key default gen_random_uuid(),
    txn_id       text        not null,
    diagnosis    text        not null,
    reason_code  text,
    explanation  text        not null,
    action_taken text        not null,
    confidence   text        not null,
    detail       jsonb       not null default '{}'::jsonb,
    embedding    vector(384),
    resolved_at  timestamptz,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),

    constraint tickets_txn_id_key unique (txn_id),

    constraint tickets_diagnosis_check check (
        diagnosis in (
            'clean',
            'ledger_gap',
            'pending',
            'anomaly',
            'amount_mismatch',
            'unknown'
        )
    ),

    constraint tickets_action_taken_check check (
        action_taken in (
            'auto_resolved',
            'ledger_entry_created',
            'escalated',
            'no_action_needed'
        )
    ),

    constraint tickets_confidence_check check (
        confidence in ('high', 'medium', 'low_flagged_for_review')
    ),

    -- Only a ledger_gap can produce a ledger write. Guards the one action that
    -- creates a financial record, independently of the calling code.
    constraint tickets_ledger_action_check check (
        action_taken <> 'ledger_entry_created' or diagnosis = 'ledger_gap'
    ),

    -- An anomaly or an unrecognised state must never be closed automatically.
    -- Escalation stays permitted for every diagnosis, since escalating is
    -- always the safe direction; auto-closing is what needs restricting.
    constraint tickets_no_silent_close_check check (
        action_taken = 'escalated'
        or diagnosis not in ('anomaly', 'unknown')
    ),

    -- Anything the deterministic layer could not classify is flagged for a
    -- human by definition.
    constraint tickets_unknown_confidence_check check (
        diagnosis <> 'unknown' or confidence = 'low_flagged_for_review'
    )
);

-- Ordered agent steps backing the live trace panel and GET /trace/{txn_id}.
--
-- `run_id` groups the steps of a single resolve invocation. Re-resolving a
-- transaction starts a new run rather than colliding with the previous one,
-- while `unique (run_id, step_number)` gives the frontend a server-assigned
-- ordering key that cannot duplicate on reconnect or re-poll.
create table if not exists agent_trace_logs (
    id           uuid primary key default gen_random_uuid(),
    run_id       uuid        not null,
    txn_id       text        not null,
    step_number  int         not null,
    step_name    text        not null,
    step_status  text        not null default 'ok',
    step_result  text,
    detail       jsonb       not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),

    constraint agent_trace_logs_run_step_key unique (run_id, step_number),
    constraint agent_trace_logs_step_number_check check (step_number > 0),
    constraint agent_trace_logs_step_status_check check (
        step_status in ('ok', 'not_found', 'skipped', 'warning', 'error')
    )
);

create index if not exists tickets_action_taken_idx
    on tickets (action_taken);

create index if not exists tickets_confidence_idx
    on tickets (confidence);

create index if not exists tickets_created_at_idx
    on tickets (created_at desc);

create index if not exists agent_trace_logs_txn_id_created_at_idx
    on agent_trace_logs (txn_id, created_at desc);

create index if not exists agent_trace_logs_run_id_step_idx
    on agent_trace_logs (run_id, step_number);

-- Keep updated_at honest without requiring callers to set it.
create or replace function set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists tickets_set_updated_at on tickets;
create trigger tickets_set_updated_at
    before update on tickets
    for each row
    execute function set_updated_at();

-- The "honest exception list" from the problem statement, as a first-class
-- object rather than a filter the UI has to remember to apply.
create or replace view exception_list as
select
    t.txn_id,
    t.diagnosis,
    t.reason_code,
    t.explanation,
    t.action_taken,
    t.confidence,
    t.detail,
    t.created_at
from tickets t
where t.confidence = 'low_flagged_for_review'
order by t.created_at desc;

-- Without security_invoker the view would run with its owner's privileges and
-- bypass row-level security on tickets. Available from Postgres 15.
do $$
begin
    if current_setting('server_version_num')::int >= 150000 then
        execute 'alter view exception_list set (security_invoker = true)';
    end if;
end
$$;

comment on table tickets is
    'One row per resolved transaction. diagnosis and confidence come from the deterministic comparison, never from the LLM.';
comment on column tickets.detail is
    'The diagnosis object behind the explanation. Every claim in explanation must be traceable to a key here.';
comment on column tickets.embedding is
    'all-MiniLM-L6-v2 embedding of explanation, for similar-case retrieval.';
comment on table agent_trace_logs is
    'Ordered agent steps per resolve run. run_id groups one invocation; step_number is server-assigned.';
comment on view exception_list is
    'Transactions the agent declined to resolve confidently. Requires human review.';
