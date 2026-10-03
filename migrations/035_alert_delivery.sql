begin;

alter table public.alert_rules
  add column delivery_destination text default null
  check (delivery_destination is null or (
    length(delivery_destination) between 1 and 64
    and delivery_destination ~ '^[a-zA-Z0-9_-]+$'
  ));

alter table public.alerts
  add column delivery_sequence bigint not null default 0 check (delivery_sequence >= 0);

create table public.alert_delivery_outbox (
  id uuid primary key default gen_random_uuid(),
  alert_id bigint not null,
  destination text not null check (
    length(destination) between 1 and 64 and destination ~ '^[a-zA-Z0-9_-]+$'
  ),
  sequence bigint not null check (sequence > 0),
  event_type text not null,
  severity text not null check (severity in ('info','warning','critical')),
  status text not null check (status in ('open','resolved')),
  previous_status text check (previous_status in ('open','acknowledged','resolved')),
  occurred_at timestamptz not null default now(),
  delivery_status text not null default 'pending'
    check (delivery_status in ('pending','delivered','dead')),
  attempts integer not null default 0 check (attempts >= 0),
  next_attempt_at timestamptz not null default now(),
  lease_token uuid,
  lease_until timestamptz,
  last_error text check (
    last_error is null or (
      length(last_error) between 1 and 64 and last_error ~ '^[a-z][a-z0-9_]*$'
    )
  ),
  last_status integer check (last_status between 100 and 599),
  unique (alert_id, sequence),
  check ((lease_token is null) = (lease_until is null)),
  check (delivery_status = 'pending' or lease_token is null)
);

create index alert_delivery_outbox_pending_idx
  on public.alert_delivery_outbox(destination, occurred_at, id)
  where delivery_status = 'pending';

alter table public.alert_delivery_outbox enable row level security;
revoke all on public.alert_delivery_outbox from public, anon, authenticated, service_role;
grant select on public.alert_delivery_outbox to service_role;
grant update (delivery_status, attempts, next_attempt_at, lease_token, lease_until,
              last_error, last_status)
  on public.alert_delivery_outbox to service_role;
create policy deny_direct_access on public.alert_delivery_outbox
  for all to public using (false) with check (false);

create function public.set_alert_delivery_sequence() returns trigger
language plpgsql set search_path = pg_catalog as $$
begin
  if TG_OP = 'INSERT' then
    NEW.delivery_sequence := case when NEW.status = 'open' then 1 else 0 end;
  elsif NEW.status is distinct from OLD.status then
    NEW.delivery_sequence := OLD.delivery_sequence + 1;
  else
    NEW.delivery_sequence := OLD.delivery_sequence;
  end if;
  return NEW;
end;
$$;

create function public.capture_alert_delivery() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  target_destination text;
  prior_status text;
begin
  if TG_OP = 'INSERT' then
    if NEW.status <> 'open' then
      return NEW;
    end if;
  else
    if NEW.status is not distinct from OLD.status or NEW.status not in ('open','resolved') then
      return NEW;
    end if;
    prior_status := OLD.status;
  end if;
  select delivery_destination into target_destination
    from public.alert_rules where id = NEW.rule_id;
  if target_destination is null then
    return NEW;
  end if;
  insert into public.alert_delivery_outbox (
    alert_id, destination, sequence, event_type, severity, status, previous_status, occurred_at
  ) values (
    NEW.id, target_destination, NEW.delivery_sequence, NEW.event_type, NEW.severity,
    NEW.status, prior_status, now()
  );
  return NEW;
end;
$$;

revoke all on function public.set_alert_delivery_sequence()
  from public, anon, authenticated, service_role;
revoke all on function public.capture_alert_delivery()
  from public, anon, authenticated, service_role;

create trigger alert_delivery_sequence
  before insert or update of status, delivery_sequence on public.alerts
  for each row execute function public.set_alert_delivery_sequence();
create trigger alert_delivery_capture
  after insert or update of status on public.alerts
  for each row execute function public.capture_alert_delivery();

commit;
