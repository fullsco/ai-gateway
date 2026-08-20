-- Settle budget reservations against what a request actually cost.
--
-- Reservation happens before dispatch, from an estimate: the input is a
-- characters-over-four heuristic and the output is whatever max_tokens the client
-- declared. Anthropic requires max_tokens, so the output side is reserved at its
-- ceiling rather than its outcome. Nothing ever adjusted the reservation
-- afterwards and nothing released it when an attempt produced no usage at all, so
-- reserved spend drifted permanently above real spend.
--
-- Measured on live traffic before this change: $9.285266 reserved against
-- $4.931146 actually spent, a factor of 1.88. A budget of $4,000 would therefore
-- have started refusing requests at about $2,124 of real spend, which is not what
-- the operator asked for.
--
-- This applies a signed correction to the current window, clamped at zero so a
-- correction can never drive a window negative.

begin;

create or replace function public.settle_gateway_budgets(
  p_client_id uuid,
  p_provider_id uuid,
  p_credential_id uuid,
  p_model_id text,
  p_route_id uuid,
  p_currency text,
  p_delta numeric
) returns void language plpgsql security definer set search_path = public as $$
declare
  budget record;
  window_start timestamptz;
begin
  if p_delta is null or p_delta = 0 or p_currency is null then return; end if;

  for budget in
    select * from gateway_budgets b where b.enabled and b.currency = upper(p_currency) and (
      b.scope_type = 'global' or
      (b.scope_type = 'client' and b.scope_id = p_client_id::text) or
      (b.scope_type = 'provider' and b.scope_id = p_provider_id::text) or
      (b.scope_type = 'credential' and b.scope_id = p_credential_id::text) or
      (b.scope_type = 'model' and b.scope_id = p_model_id) or
      (b.scope_type = 'route' and b.scope_id = p_route_id::text)
    ) order by b.id for update
  loop
    window_start := case budget.period when 'daily' then date_trunc('day', now())
      else date_trunc('month', now()) end;
    update budget_usage_windows
       set reserved_cost = greatest(0, reserved_cost + p_delta),
           updated_at = now()
     where budget_id = budget.id and window_started_at = window_start;
  end loop;
end;
$$;

revoke all on function public.settle_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  from public, anon, authenticated;
grant execute on function public.settle_gateway_budgets(uuid,uuid,uuid,text,uuid,text,numeric)
  to service_role;

-- The same correction is needed for a client spending limit, which reserves from
-- the same estimate.
create or replace function public.settle_client_spending(
  p_client_id uuid,
  p_delta numeric
) returns void language plpgsql security definer set search_path = public as $$
declare
  current_window timestamptz := date_trunc('month', now());
begin
  if p_delta is null or p_delta = 0 then return; end if;
  update client_spend_windows
     set reserved_cost = greatest(0, reserved_cost + p_delta), updated_at = now()
   where client_id = p_client_id and window_started_at = current_window;
end;
$$;

revoke all on function public.settle_client_spending(uuid,numeric)
  from public, anon, authenticated;
grant execute on function public.settle_client_spending(uuid,numeric) to service_role;

commit;
