create table public.workflow_jobs (
  id uuid primary key,
  client_id uuid not null references public.gateway_clients(id),
  idempotency_key text not null check (length(idempotency_key) between 1 and 200),
  input_hash text not null,
  input jsonb not null,
  checkpoint jsonb not null default '{}',
  result jsonb,
  status text not null default 'queued' check (status in
    ('queued','running','waiting_for_tool','waiting_for_approval',
     'completed','failed','cancelled','expired')),
  version bigint not null default 0,
  lease_owner uuid,
  lease_expires_at timestamptz,
  deadline timestamptz not null,
  attempts integer not null default 0,
  max_attempts integer not null default 3 check (max_attempts between 1 and 10),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(client_id,idempotency_key)
);
create table public.workflow_events (
  job_id uuid not null references public.workflow_jobs(id) on delete cascade,
  sequence bigint not null,
  kind text not null,
  data jsonb not null default '{}',
  created_at timestamptz not null default now(),
  primary key(job_id,sequence)
);
create table public.workflow_tool_results (
  job_id uuid not null references public.workflow_jobs(id) on delete cascade,
  invocation_id text not null,
  input_hash text not null,
  result jsonb not null,
  created_at timestamptz not null default now(),
  primary key(job_id,invocation_id)
);
create index workflow_jobs_claim_idx on public.workflow_jobs(created_at)
  where status='queued';
create index workflow_jobs_lease_idx on public.workflow_jobs(lease_expires_at)
  where status='running';
create index workflow_jobs_client_idx on public.workflow_jobs(client_id,created_at desc);
alter table public.workflow_jobs enable row level security;
alter table public.workflow_events enable row level security;
alter table public.workflow_tool_results enable row level security;
revoke all on public.workflow_jobs, public.workflow_events, public.workflow_tool_results
  from public,anon,authenticated;
grant select,insert,update,delete on public.workflow_jobs,public.workflow_events,
  public.workflow_tool_results to service_role;
