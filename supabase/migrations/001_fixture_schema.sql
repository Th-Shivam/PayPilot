create extension if not exists vector;

create table if not exists public.gateway_records (
  transaction_id text primary key,
  amount numeric(12, 2) not null,
  currency text not null,
  occurred_at timestamptz not null,
  status text not null,
  expected_settlement_at timestamptz not null
);

create table if not exists public.bank_settlements (
  transaction_id text primary key,
  amount numeric(12, 2) not null,
  currency text not null,
  occurred_at timestamptz not null,
  status text not null,
  expected_settlement_at timestamptz not null
);

create table if not exists public.ledger_records (
  transaction_id text primary key,
  amount numeric(12, 2) not null,
  currency text not null,
  occurred_at timestamptz not null,
  status text not null,
  expected_settlement_at timestamptz not null
);

create table if not exists public.tickets (
  ticket_id text primary key,
  transaction_id text not null,
  status text not null,
  explanation text not null,
  resolution_path text not null,
  explanation_embedding vector(384),
  created_at timestamptz not null default now()
);
