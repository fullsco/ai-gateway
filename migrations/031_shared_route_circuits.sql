create table public.route_circuits (
  route_id text primary key,
  failures integer not null default 0 check(failures>=0),
  successes integer not null default 0 check(successes>=0),
  state text not null default 'closed' check(state in ('closed','open','half_open')),
  retry_at timestamptz,
  probe_owner uuid,
  probe_expires_at timestamptz,
  updated_at timestamptz not null default now()
);
alter table public.route_circuits enable row level security;
revoke all on public.route_circuits from public,anon,authenticated;
grant select,insert,update,delete on public.route_circuits to service_role;
