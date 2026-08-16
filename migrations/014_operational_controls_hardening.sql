begin;

create or replace function public.reserve_gateway_budgets(
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
    select * from gateway_budgets b where b.enabled
      and b.enforcement='block' and b.currency=upper(p_currency) and (
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
    if used+p_estimated_cost > budget.limit_amount then return budget.id::text; end if;
  end loop;

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
    update budget_usage_windows set reserved_cost=reserved_cost+p_estimated_cost,
      request_count=request_count+1,updated_at=now()
      where budget_id=budget.id and window_started_at=window_start;
  end loop;
  return null;
end;
$$;

revoke all on function public.reserve_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  from public,anon,authenticated;
grant execute on function public.reserve_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  to service_role;

create index if not exists alerts_acknowledged_by_idx on public.alerts(acknowledged_by);
create index if not exists provider_pools_model_idx on public.provider_pools(model_id);

commit;
