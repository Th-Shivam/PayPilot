-- 0005_rls_policies.sql
-- Row Level Security for every table.
--
-- Why RLS is enabled now rather than deferred to the auth phase
-- -------------------------------------------------------------
-- The dashboard (issue #9) reads Supabase REST directly from the browser using
-- the anon key, which never passes through FastAPI. Backend token validation
-- does nothing on that path; RLS is the only thing that constrains it.
--
-- Enabling RLS later is also a trap: Postgres with RLS on and no matching
-- policy returns an empty result set with HTTP 200, not an error. The dashboard
-- would silently render an empty table with nothing in the logs.
--
-- Current posture
-- ---------------
--   tickets, agent_trace_logs, exception_list : readable by anon + authenticated
--   gateway_transactions, bank_settlements, ledger_entries : no anon access
--   all writes : service role only
--
-- The backend uses SUPABASE_SERVICE_ROLE_KEY, which carries BYPASSRLS, so no
-- write policies are needed for the agent to function.
--
-- Phase 5 (issue #13) tightening: replace the permissive read policies below
-- with owner-scoped equivalents, e.g.
--
--   create policy tickets_read_own on tickets for select to authenticated
--   using (
--       exists (
--           select 1 from gateway_transactions g
--           where g.txn_id = tickets.txn_id and g.owner_id = auth.uid()
--       )
--       or coalesce(auth.jwt() -> 'app_metadata' ->> 'role', '') = 'support_agent'
--   );
--
-- Because RLS is already on, that change edits a policy instead of flipping
-- table-level enforcement, so it cannot silently break reads.

alter table gateway_transactions enable row level security;
alter table bank_settlements     enable row level security;
alter table ledger_entries       enable row level security;
alter table tickets              enable row level security;
alter table agent_trace_logs     enable row level security;

-- Dashboard and trace panel reads.
drop policy if exists tickets_read_all on tickets;
create policy tickets_read_all
    on tickets
    for select
    to anon, authenticated
    using (true);

drop policy if exists agent_trace_logs_read_all on agent_trace_logs;
create policy agent_trace_logs_read_all
    on agent_trace_logs
    for select
    to anon, authenticated
    using (true);

-- The three source feeds carry customer names and settlement detail and are
-- only ever read by the backend, which bypasses RLS. No client policy is
-- created, so RLS denies client access by default.

revoke all on gateway_transactions from anon, authenticated;
revoke all on bank_settlements     from anon, authenticated;
revoke all on ledger_entries       from anon, authenticated;

grant select on tickets          to anon, authenticated;
grant select on agent_trace_logs to anon, authenticated;
grant select on exception_list   to anon, authenticated;

grant execute on function match_tickets(vector, float, int, text) to anon, authenticated;
