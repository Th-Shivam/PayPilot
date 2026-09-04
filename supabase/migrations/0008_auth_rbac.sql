-- 0008_auth_rbac.sql
-- Supabase Auth profiles, role-aware RLS, and transaction ownership.
--
-- Ownership path:
--   auth.users.id -> gateway_transactions.owner_id -> txn_id
--   -> tickets / agent_trace_logs
--
-- The backend uses the service-role client and therefore continues to bypass
-- these policies. Browser access uses the user's JWT and is constrained here.

create table if not exists public.profiles (
    id         uuid primary key,
    role       text not null default 'business_owner',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint profiles_role_check
        check (role in ('support_agent', 'business_owner'))
);

-- Keep this migration usable in schema-only local Postgres checks while adding
-- the Auth foreign key whenever the Supabase auth schema is available.
do $$
begin
    if exists (
        select 1 from information_schema.tables
        where table_schema = 'auth' and table_name = 'users'
    ) and not exists (
        select 1 from information_schema.table_constraints
        where constraint_schema = 'public'
          and table_name = 'profiles'
          and constraint_name = 'profiles_id_fkey'
    ) then
        alter table public.profiles
            add constraint profiles_id_fkey
            foreign key (id) references auth.users (id) on delete cascade;
    end if;
end
$$;

alter table public.gateway_transactions add column if not exists owner_id uuid;
create index if not exists profiles_role_idx on public.profiles (role);
create index if not exists tickets_txn_id_idx on public.tickets (txn_id);
create index if not exists agent_trace_logs_txn_id_idx on public.agent_trace_logs (txn_id);

-- New email/password or magic-link users get the least-privileged role. A
-- support agent must be provisioned by an administrator/service-role process.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.profiles (id, role)
    values (new.id, 'business_owner')
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists paypilot_on_auth_user_created on auth.users;
create trigger paypilot_on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

create or replace function public.is_support_agent()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from public.profiles
        where id = auth.uid() and role = 'support_agent'
    );
$$;

grant execute on function public.is_support_agent() to authenticated;

alter table public.profiles enable row level security;
alter table public.gateway_transactions enable row level security;
alter table public.bank_settlements enable row level security;
alter table public.ledger_entries enable row level security;
alter table public.tickets enable row level security;
alter table public.agent_trace_logs enable row level security;

-- Remove the phase-2 broad policies and all anonymous reads. Anonymous users
-- must authenticate before they can see even an empty business table.
drop policy if exists tickets_read_all on public.tickets;
drop policy if exists agent_trace_logs_read_all on public.agent_trace_logs;
drop policy if exists profiles_read_own on public.profiles;
drop policy if exists gateway_transactions_read_role_scoped on public.gateway_transactions;
drop policy if exists bank_settlements_read_role_scoped on public.bank_settlements;
drop policy if exists ledger_entries_read_role_scoped on public.ledger_entries;
drop policy if exists tickets_read_role_scoped on public.tickets;
drop policy if exists agent_trace_logs_read_role_scoped on public.agent_trace_logs;

revoke all on public.profiles from anon;
revoke all on public.gateway_transactions from anon, authenticated;
revoke all on public.bank_settlements from anon, authenticated;
revoke all on public.ledger_entries from anon, authenticated;
revoke all on public.tickets from anon, authenticated;
revoke all on public.agent_trace_logs from anon, authenticated;
revoke select on public.exception_list from anon;
revoke execute on function match_tickets(vector, float, int, text) from anon;

grant select on public.profiles to authenticated;
grant select on public.gateway_transactions to authenticated;
grant select on public.bank_settlements to authenticated;
grant select on public.ledger_entries to authenticated;
grant select on public.tickets to authenticated;
grant select on public.agent_trace_logs to authenticated;
grant select on public.exception_list to authenticated;
grant execute on function match_tickets(vector, float, int, text) to authenticated;

create policy profiles_read_own
    on public.profiles for select to authenticated
    using (id = auth.uid());

create policy gateway_transactions_read_role_scoped
    on public.gateway_transactions for select to authenticated
    using (owner_id = auth.uid() or public.is_support_agent());

create policy bank_settlements_read_role_scoped
    on public.bank_settlements for select to authenticated
    using (
        public.is_support_agent()
        or exists (
            select 1 from public.gateway_transactions g
            where g.txn_id = bank_settlements.txn_id
              and g.owner_id = auth.uid()
        )
    );

create policy ledger_entries_read_role_scoped
    on public.ledger_entries for select to authenticated
    using (
        public.is_support_agent()
        or exists (
            select 1 from public.gateway_transactions g
            where g.txn_id = ledger_entries.txn_id
              and g.owner_id = auth.uid()
        )
    );

create policy tickets_read_role_scoped
    on public.tickets for select to authenticated
    using (
        public.is_support_agent()
        or exists (
            select 1 from public.gateway_transactions g
            where g.txn_id = tickets.txn_id
              and g.owner_id = auth.uid()
        )
    );

create policy agent_trace_logs_read_role_scoped
    on public.agent_trace_logs for select to authenticated
    using (
        public.is_support_agent()
        or exists (
            select 1 from public.gateway_transactions g
            where g.txn_id = agent_trace_logs.txn_id
              and g.owner_id = auth.uid()
        )
    );

comment on table public.profiles is
    'Server-owned PayPilot role for an auth.users identity. Clients cannot write role.';
comment on column public.gateway_transactions.owner_id is
    'auth.users.id owning this transaction; tickets and traces inherit access through txn_id.';
