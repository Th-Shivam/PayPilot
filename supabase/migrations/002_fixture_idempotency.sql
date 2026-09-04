-- Business records must be unique by transaction_id for development upserts.
create unique index if not exists gateway_records_transaction_id_uidx
  on public.gateway_records (transaction_id);
create unique index if not exists bank_settlements_transaction_id_uidx
  on public.bank_settlements (transaction_id);
create unique index if not exists ledger_records_transaction_id_uidx
  on public.ledger_records (transaction_id);

create unique index if not exists tickets_ticket_id_uidx
  on public.tickets (ticket_id);
