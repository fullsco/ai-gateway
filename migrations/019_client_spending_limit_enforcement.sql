-- Per-client spending limit enforcement.
--
-- gateway_clients.spending_limit was stored, published into the runtime snapshot,
-- returned by the admin API and rendered in the dashboard, but no code ever read
-- it: a client spend cap that did nothing. This adds the monthly window and the
-- atomic reservation needed to actually hold it.
--
-- The reservation is deliberately modelled on reserve_gateway_budgets: check
-- before charging, take a row lock so concurrent requests cannot both slip under
-- the limit, and return the offending scope rather than raising.

begin;

create table if not exists public.client_spend_windows (
  client_id uuid not null references public.gateway_clients(id) on delete cascade,
  window_started_at timestamptz not null,
  reserved_cost numeric(18,8) not null default 0 check (reserved_cost >= 0),
  request_count integer not null default 0 check (request_count >= 0),
  updated_at timestamptz not null default now(),
  primary key (client_id, window_started_at)
);

comment on table public.client_spend_windows is
  'Monthly spend reserved per client, used to enforce gateway_clients.spending_limit.';

create index if not exists client_spend_windows_time_idx
  on public.client_spend_windows(window_started_at);

create or replace function public.reserve_client_spending(
  p_client_id uuid,
  p_estimated_cost numeric
) returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  current_window timestamptz := date_trunc('month', now());
  limit_amount numeric;
  used numeric;
begin
  -- An unpriced request cannot be charged against a spend limit here. The
  -- unpriced-route guard in the application refuses those separately, so this
  -- returning null is not a way around the cap.
  if p_estimated_cost is null then return null; end if;
  if p_estimated_cost < 0 then return 'invalid_cost'; end if;

  select c.spending_limit into limit_amount
    from gateway_clients c where c.id = p_client_id;
  if limit_amount is null then return null; end if;

  insert into client_spend_windows(client_id, window_started_at)
  values (p_client_id, current_window)
  on conflict (client_id, window_started_at) do nothing;

  select reserved_cost into used from client_spend_windows
   where client_id = p_client_id and window_started_at = current_window
   for update;

  if used + p_estimated_cost > limit_amount then
    return 'spending_limit';
  end if;

  update client_spend_windows
     set reserved_cost = reserved_cost + p_estimated_cost,
         request_count = request_count + 1,
         updated_at = now()
   where client_id = p_client_id and window_started_at = current_window;

  return null;
end;
$$;

alter table public.client_spend_windows enable row level security;
create policy deny_direct_access on public.client_spend_windows
  for all to public using (false) with check (false);

revoke all on function public.reserve_client_spending(uuid, numeric)
  from public, anon, authenticated;
grant execute on function public.reserve_client_spending(uuid, numeric) to service_role;

commit;
