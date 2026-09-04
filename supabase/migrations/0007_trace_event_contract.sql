-- Canonical trace event fields shared by live SSE, persistence, and replay.
-- Existing step_* columns remain for compatibility with already deployed code.

alter table agent_trace_logs add column if not exists event_id text;
alter table agent_trace_logs add column if not exists event_type text;
alter table agent_trace_logs add column if not exists status text;
alter table agent_trace_logs add column if not exists summary text;
alter table agent_trace_logs add column if not exists event_timestamp timestamptz;

update agent_trace_logs
set event_id = coalesce(event_id, run_id::text || ':' || step_number::text),
    event_type = coalesce(
        event_type,
        case
            when step_name in ('diagnosis', 'compare_records') then 'decision'
            when step_name like 'tool_call:%' then 'tool_start'
            when step_name like 'tool_result:%' then 'tool_result'
            else 'tool_result'
        end
    ),
    status = coalesce(
        status,
        case step_status
            when 'ok' then 'success'
            when 'not_found' then 'not_found'
            when 'warning' then 'warning'
            when 'error' then 'failed'
            else 'warning'
        end
    ),
    summary = coalesce(summary, nullif(step_result, ''), step_name),
    event_timestamp = coalesce(event_timestamp, created_at)
where event_id is null
   or event_type is null
   or status is null
   or summary is null
   or event_timestamp is null;

alter table agent_trace_logs alter column event_id set not null;
alter table agent_trace_logs alter column event_type set not null;
alter table agent_trace_logs alter column status set not null;
alter table agent_trace_logs alter column summary set not null;
alter table agent_trace_logs alter column event_timestamp set not null;

alter table agent_trace_logs drop constraint if exists agent_trace_logs_event_type_check;
alter table agent_trace_logs add constraint agent_trace_logs_event_type_check check (
    event_type in ('tool_start', 'tool_result', 'decision', 'action', 'retry', 'completion')
);

alter table agent_trace_logs drop constraint if exists agent_trace_logs_status_check;
alter table agent_trace_logs add constraint agent_trace_logs_status_check check (
    status in ('running', 'success', 'warning', 'not_found', 'failed', 'completed')
);

create unique index if not exists agent_trace_logs_event_id_idx
    on agent_trace_logs (event_id);

comment on column agent_trace_logs.event_id is
    'Stable run-scoped event identity used for SSE reconnect and replay deduplication.';
comment on column agent_trace_logs.event_type is
    'Canonical event type shared by SSE and GET /trace.';
