alter table ledger_entries add column if not exists action_key text;
alter table tickets add column if not exists action_key text;
create unique index if not exists ledger_entries_action_key_idx on ledger_entries (action_key) where action_key is not null;
create unique index if not exists tickets_action_key_idx on tickets (action_key) where action_key is not null;
