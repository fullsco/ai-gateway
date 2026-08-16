begin;

create table public.provider_pools (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  model_id text references public.models(id) on delete cascade,
  enabled boolean not null default true,
  strategy text not null default 'priority'
    check (strategy in ('priority','weighted','least_loaded')),
  settings jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.provider_pool_members (
  pool_id uuid not null references public.provider_pools(id) on delete cascade,
  provider_model_id uuid not null references public.provider_models(id) on delete cascade,
  credential_id uuid not null references public.provider_credentials(id) on delete cascade,
  enabled boolean not null default true,
  draining boolean not null default false,
  priority integer not null default 100 check (priority >= 0),
  weight numeric not null default 1 check (weight > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (pool_id, provider_model_id, credential_id)
);

alter table public.model_routes
  add column pool_id uuid references public.provider_pools(id) on delete set null;

create table public.credential_quota_windows (
  credential_id uuid not null references public.provider_credentials(id) on delete cascade,
  window_started_at timestamptz not null,
  request_count integer not null default 0 check (request_count >= 0),
  token_count bigint not null default 0 check (token_count >= 0),
  primary key (credential_id, window_started_at)
);

create function public.reserve_provider_credential_quota(
  p_credential_id uuid,
  p_requests_per_minute integer,
  p_tokens_per_minute bigint,
  p_estimated_tokens bigint
) returns text language plpgsql security definer set search_path = public as $$
declare
  current_window timestamptz := date_trunc('minute', now());
  current_requests integer;
  current_tokens bigint;
begin
  if p_estimated_tokens < 0 then return 'tokens'; end if;
  insert into credential_quota_windows(credential_id,window_started_at)
    values(p_credential_id,current_window) on conflict do nothing;
  select request_count,token_count into current_requests,current_tokens
    from credential_quota_windows
    where credential_id=p_credential_id and window_started_at=current_window for update;
  if p_requests_per_minute is not null and current_requests + 1 > p_requests_per_minute
    then return 'requests'; end if;
  if p_tokens_per_minute is not null and current_tokens + p_estimated_tokens > p_tokens_per_minute
    then return 'tokens'; end if;
  update credential_quota_windows set request_count=request_count+1,
    token_count=token_count+p_estimated_tokens
    where credential_id=p_credential_id and window_started_at=current_window;
  return null;
end;
$$;

create table public.gateway_budgets (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  scope_type text not null check (scope_type in ('global','client','provider','credential','model','route')),
  scope_id text,
  period text not null default 'monthly' check (period in ('daily','monthly')),
  currency text not null check (length(currency)=3),
  limit_amount numeric(18,8) not null check (limit_amount >= 0),
  warning_threshold numeric(5,4) not null default 0.8
    check (warning_threshold > 0 and warning_threshold <= 1),
  enforcement text not null default 'warn' check (enforcement in ('warn','block')),
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((scope_type='global' and scope_id is null) or (scope_type<>'global' and scope_id is not null))
);

create table public.budget_usage_windows (
  budget_id uuid not null references public.gateway_budgets(id) on delete cascade,
  window_started_at timestamptz not null,
  reserved_cost numeric(18,8) not null default 0 check (reserved_cost >= 0),
  request_count bigint not null default 0 check (request_count >= 0),
  updated_at timestamptz not null default now(),
  primary key (budget_id,window_started_at)
);

create table public.alert_rules (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  enabled boolean not null default true,
  severity text not null default 'warning' check (severity in ('info','warning','critical')),
  event_type text not null,
  scope_type text,
  scope_id text,
  condition jsonb not null default '{}',
  cooldown_seconds integer not null default 300 check (cooldown_seconds >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.alerts (
  id bigint generated always as identity primary key,
  rule_id uuid references public.alert_rules(id) on delete set null,
  dedup_key text not null,
  severity text not null check (severity in ('info','warning','critical')),
  status text not null default 'open' check (status in ('open','acknowledged','resolved')),
  event_type text not null,
  title text not null,
  scope_type text,
  scope_id text,
  metadata jsonb not null default '{}',
  occurrence_count bigint not null default 1,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  acknowledged_by uuid references auth.users(id) on delete set null,
  acknowledged_at timestamptz,
  resolved_at timestamptz
);

create unique index alerts_open_dedup_idx on public.alerts(dedup_key)
  where status in ('open','acknowledged');

create function public.reserve_gateway_budgets(
  p_client_id uuid,
  p_provider_id uuid,
  p_credential_id uuid,
  p_model_id text,
  p_route_id uuid,
  p_currency text,
  p_estimated_cost numeric
) returns text language plpgsql security definer set search_path = public as $$
declare
  budget record;
  window_start timestamptz;
  used numeric;
begin
  if p_estimated_cost is null or p_currency is null then return null; end if;
  if p_estimated_cost < 0 then return 'invalid_cost'; end if;
  for budget in
    select * from gateway_budgets b where b.enabled and b.currency=upper(p_currency) and (
      b.scope_type='global' or
      (b.scope_type='client' and b.scope_id=p_client_id::text) or
      (b.scope_type='provider' and b.scope_id=p_provider_id::text) or
      (b.scope_type='credential' and b.scope_id=p_credential_id::text) or
      (b.scope_type='model' and b.scope_id=p_model_id) or
      (b.scope_type='route' and b.scope_id=p_route_id::text)
    ) order by b.id for update
  loop
    window_start := case budget.period when 'daily' then date_trunc('day',now())
      else date_trunc('month',now()) end;
    insert into budget_usage_windows(budget_id,window_started_at)
      values(budget.id,window_start) on conflict do nothing;
    select reserved_cost into used from budget_usage_windows
      where budget_id=budget.id and window_started_at=window_start for update;
    update budget_usage_windows set reserved_cost=reserved_cost+p_estimated_cost,
      request_count=request_count+1,updated_at=now()
      where budget_id=budget.id and window_started_at=window_start;
  end loop;
  return null;
end;
$$;

alter table public.gateway_client_keys
  add column label text,
  add column revoke_reason text;

alter table public.request_logs add column key_id uuid
  references public.gateway_client_keys(id) on delete set null;

create index provider_pool_members_mapping_idx on public.provider_pool_members(provider_model_id);
create index provider_pool_members_credential_idx on public.provider_pool_members(credential_id);
create index model_routes_pool_idx on public.model_routes(pool_id);
create index credential_quota_windows_time_idx on public.credential_quota_windows(window_started_at);
create index budget_usage_windows_time_idx on public.budget_usage_windows(window_started_at);
create index gateway_budgets_scope_idx on public.gateway_budgets(scope_type,scope_id,enabled);
create index alerts_status_time_idx on public.alerts(status,last_seen_at desc);
create index alerts_rule_idx on public.alerts(rule_id);
create index request_logs_key_time_idx on public.request_logs(key_id,started_at desc);

alter table public.provider_pools enable row level security;
alter table public.provider_pool_members enable row level security;
alter table public.credential_quota_windows enable row level security;
alter table public.gateway_budgets enable row level security;
alter table public.budget_usage_windows enable row level security;
alter table public.alert_rules enable row level security;
alter table public.alerts enable row level security;
create policy deny_direct_access on public.provider_pools for all to public using(false) with check(false);
create policy deny_direct_access on public.provider_pool_members for all to public using(false) with check(false);
create policy deny_direct_access on public.credential_quota_windows for all to public using(false) with check(false);
create policy deny_direct_access on public.gateway_budgets for all to public using(false) with check(false);
create policy deny_direct_access on public.budget_usage_windows for all to public using(false) with check(false);
create policy deny_direct_access on public.alert_rules for all to public using(false) with check(false);
create policy deny_direct_access on public.alerts for all to public using(false) with check(false);

revoke all on function public.reserve_provider_credential_quota(uuid,integer,bigint,bigint)
  from public,anon,authenticated;
revoke all on function public.reserve_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  from public,anon,authenticated;
grant execute on function public.reserve_provider_credential_quota(uuid,integer,bigint,bigint)
  to service_role;
grant execute on function public.reserve_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  to service_role;

commit;
