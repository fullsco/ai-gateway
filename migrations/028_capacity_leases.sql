create table public.capacity_scopes (
  scope text primary key,
  capacity integer not null check (capacity > 0),
  updated_at timestamptz not null default now()
);
create table public.capacity_leases (
  owner uuid not null,
  scope text not null references public.capacity_scopes(scope),
  expires_at timestamptz not null,
  primary key(owner,scope)
);
create index capacity_leases_scope_expiry_idx on public.capacity_leases(scope,expires_at);
alter table public.capacity_scopes enable row level security;
alter table public.capacity_leases enable row level security;
revoke all on public.capacity_scopes,public.capacity_leases from public,anon,authenticated;
grant select,insert,update,delete on public.capacity_scopes,public.capacity_leases to service_role;
