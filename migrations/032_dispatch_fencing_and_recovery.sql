alter table public.financial_reservations add column workflow_owner uuid;
alter table public.financial_reservations add column workflow_version bigint;
alter table public.financial_reservations add column telemetry_request_id text;
alter table public.financial_reservations add column estimated_tokens bigint not null default 0;
alter table public.financial_reservations add column actual_tokens bigint;
alter table public.settlement_outbox add column attempts integer not null default 0;
alter table public.settlement_outbox add column next_attempt_at timestamptz not null default now();
create index settlement_outbox_due_idx on public.settlement_outbox(next_attempt_at)
  where applied_at is null;
alter table public.route_circuits add column generation bigint not null default 0;
