create table public.workflow_artifacts (
  id uuid primary key,
  client_id uuid not null references public.gateway_clients(id),
  job_id uuid not null references public.workflow_jobs(id) on delete cascade,
  sha256 text not null,
  size_bytes integer not null check(size_bytes between 0 and 1048576),
  nonce bytea not null,
  ciphertext bytea not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  check(octet_length(nonce)=12),
  check(octet_length(ciphertext)=size_bytes+16)
);
create index workflow_artifacts_client_idx on public.workflow_artifacts(client_id);
create index workflow_artifacts_job_idx on public.workflow_artifacts(job_id);
create index workflow_artifacts_expiry_idx on public.workflow_artifacts(expires_at);
alter table public.workflow_artifacts enable row level security;
revoke all on public.workflow_artifacts from public,anon,authenticated;
grant select,insert,update,delete on public.workflow_artifacts to service_role;
