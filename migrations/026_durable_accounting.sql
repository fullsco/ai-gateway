-- Additive development migration. Existing accounting functions remain compatible.
create table public.financial_reservations (
  id uuid primary key,
  request_id text not null,
  attempt_number integer not null check (attempt_number > 0),
  client_id uuid not null references public.gateway_clients(id),
  provider_id uuid not null references public.providers(id),
  credential_id uuid not null references public.provider_credentials(id),
  model_id text not null,
  route_id uuid,
  currency text,
  estimated_cost numeric(18,8) check (estimated_cost >= 0),
  actual_cost numeric(18,8) check (actual_cost >= 0),
  client_window timestamptz,
  status text not null default 'reserved'
    check (status in ('reserved','dispatched','unknown','settlement_pending','settled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (request_id, attempt_number)
);
create table public.financial_reservation_windows (
  reservation_id uuid not null references public.financial_reservations(id) on delete cascade,
  budget_id uuid not null references public.gateway_budgets(id),
  window_started_at timestamptz not null,
  primary key (reservation_id, budget_id)
);
create table public.settlement_outbox (
  reservation_id uuid primary key references public.financial_reservations(id),
  actual_cost numeric(18,8) not null check (actual_cost >= 0),
  created_at timestamptz not null default now(),
  applied_at timestamptz
);
create index financial_reservations_pending_idx on public.financial_reservations(updated_at)
  where status <> 'settled';
create index settlement_outbox_pending_idx on public.settlement_outbox(created_at)
  where applied_at is null;
create index financial_reservations_client_idx on public.financial_reservations(client_id);
create index financial_reservations_provider_idx on public.financial_reservations(provider_id);
create index financial_reservations_credential_idx on public.financial_reservations(credential_id);
create index financial_reservation_windows_budget_idx
  on public.financial_reservation_windows(budget_id);
alter table public.financial_reservations enable row level security;
alter table public.financial_reservation_windows enable row level security;
alter table public.settlement_outbox enable row level security;
revoke all on public.financial_reservations, public.financial_reservation_windows,
  public.settlement_outbox from public, anon, authenticated;
grant select, insert, update, delete on public.financial_reservations,
  public.financial_reservation_windows, public.settlement_outbox to service_role;
