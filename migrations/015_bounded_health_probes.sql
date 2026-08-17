begin;

create table public.health_probe_state (
  credential_id uuid primary key references public.provider_credentials(id) on delete cascade,
  provider_id uuid not null references public.providers(id) on delete cascade,
  provider_model_id uuid references public.provider_models(id) on delete set null,
  window_started_at date not null default current_date,
  request_count integer not null default 0 check (request_count >= 0),
  consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
  last_attempt_at timestamptz,
  next_allowed_at timestamptz,
  lease_until timestamptz,
  reservation_token uuid,
  manual_request_count integer not null default 0 check (manual_request_count >= 0),
  manual_last_attempt_at timestamptz,
  last_result text,
  updated_at timestamptz not null default now()
);

alter table public.health_checks
  add column source text not null default 'unknown'
    check (source in ('automatic','manual','passive','unknown'));

create function public.reserve_health_probe(
  p_provider_id uuid,
  p_credential_id uuid,
  p_provider_model_id uuid,
  p_daily_limit integer,
  p_min_interval_seconds integer,
  p_lease_seconds integer,
  p_manual boolean default false,
  p_manual_daily_limit integer default 20,
  p_manual_min_interval_seconds integer default 60
) returns text language plpgsql security definer set search_path = public as $$
declare
  state public.health_probe_state%rowtype;
  token uuid;
begin
  insert into public.health_probe_state(credential_id,provider_id,provider_model_id)
  values(p_credential_id,p_provider_id,p_provider_model_id)
  on conflict(credential_id) do nothing;

  select * into state from public.health_probe_state
  where credential_id=p_credential_id for update;

  if state.window_started_at < current_date then
    update public.health_probe_state set
      window_started_at=current_date,request_count=0,manual_request_count=0,updated_at=now()
    where credential_id=p_credential_id returning * into state;
  end if;

  if state.lease_until is not null and state.lease_until > now() then
    return 'in_progress';
  end if;
  if p_manual and state.manual_request_count >= p_manual_daily_limit then
    return 'manual_daily_limit';
  end if;
  if p_manual and state.manual_last_attempt_at is not null
     and state.manual_last_attempt_at > now()-make_interval(secs=>p_manual_min_interval_seconds) then
    return 'manual_cooldown';
  end if;
  if not p_manual and state.request_count >= p_daily_limit then
    return 'daily_limit';
  end if;
  if not p_manual and state.next_allowed_at is not null
     and state.next_allowed_at > now() then
    return 'cooldown';
  end if;

  token := gen_random_uuid();
  update public.health_probe_state set
    provider_id=p_provider_id,
    provider_model_id=p_provider_model_id,
    request_count=request_count+case when p_manual then 0 else 1 end,
    manual_request_count=manual_request_count+case when p_manual then 1 else 0 end,
    manual_last_attempt_at=case when p_manual then now() else manual_last_attempt_at end,
    last_attempt_at=now(),
    next_allowed_at=now()+make_interval(secs=>p_min_interval_seconds),
    lease_until=now()+make_interval(secs=>p_lease_seconds),
    reservation_token=token,
    updated_at=now()
  where credential_id=p_credential_id;
  return 'reserved:' || token::text;
end;
$$;

create function public.complete_health_probe(
  p_credential_id uuid,
  p_success boolean,
  p_min_interval_seconds integer,
  p_failure_backoff_seconds integer,
  p_max_backoff_seconds integer,
  p_result text,
  p_reservation_token uuid
) returns void language plpgsql security definer set search_path = public as $$
declare
  failures integer;
  delay_seconds integer;
begin
  select case when p_success then 0 else consecutive_failures+1 end
  into failures from public.health_probe_state
  where credential_id=p_credential_id
    and reservation_token=p_reservation_token
    and lease_until is not null for update;

  if failures is null then
    return;
  end if;

  delay_seconds := case when p_success then p_min_interval_seconds else least(
    p_max_backoff_seconds,
    p_failure_backoff_seconds * power(2,least(greatest(failures-1,0),10))::integer
  ) end;

  update public.health_probe_state set
    consecutive_failures=failures,
    next_allowed_at=now()+make_interval(secs=>delay_seconds),
    lease_until=null,reservation_token=null,
    last_result=p_result,
    updated_at=now()
  where credential_id=p_credential_id and reservation_token=p_reservation_token;
end;
$$;

create index health_probe_state_provider_idx
  on public.health_probe_state(provider_id,next_allowed_at);
create index health_probe_state_provider_model_idx
  on public.health_probe_state(provider_model_id);

alter table public.health_probe_state enable row level security;
create policy deny_direct_access on public.health_probe_state for all to public
  using(false) with check(false);

revoke all on function public.reserve_health_probe(
  uuid,uuid,uuid,integer,integer,integer,boolean,integer,integer
) from public;
revoke all on function public.reserve_health_probe(
  uuid,uuid,uuid,integer,integer,integer,boolean,integer,integer
) from anon,authenticated;
revoke all on function public.complete_health_probe(
  uuid,boolean,integer,integer,integer,text,uuid
) from public;
revoke all on function public.complete_health_probe(
  uuid,boolean,integer,integer,integer,text,uuid
) from anon,authenticated;
grant execute on function public.reserve_health_probe(
  uuid,uuid,uuid,integer,integer,integer,boolean,integer,integer
) to postgres,service_role;
grant execute on function public.complete_health_probe(
  uuid,boolean,integer,integer,integer,text,uuid
) to postgres,service_role;

commit;
