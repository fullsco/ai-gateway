begin;

create extension if not exists pgcrypto;

create type public.gateway_health_state as enum (
  'healthy', 'degraded', 'rate_limited', 'auth_failed',
  'quota_exhausted', 'unavailable', 'cooldown', 'disabled'
);

create table public.gateway_clients (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  enabled boolean not null default true,
  allowed_protocols text[] not null default '{}',
  allowed_models text[] not null default '{}',
  requests_per_minute integer check (requests_per_minute is null or requests_per_minute > 0),
  tokens_per_minute bigint check (tokens_per_minute is null or tokens_per_minute > 0),
  spending_limit numeric(18, 8) check (spending_limit is null or spending_limit >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.gateway_client_keys (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references public.gateway_clients(id) on delete cascade,
  key_prefix text not null unique,
  key_digest text not null,
  enabled boolean not null default true,
  last_used_at timestamptz,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);

create table public.providers (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  provider_type text not null,
  protocol text not null,
  base_url text not null,
  enabled boolean not null default true,
  priority integer not null default 100,
  capabilities text[] not null default '{}',
  timeout_seconds numeric not null default 600 check (timeout_seconds > 0),
  settings jsonb not null default '{}',
  health public.gateway_health_state not null default 'healthy',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.provider_credentials (
  id uuid primary key default gen_random_uuid(),
  provider_id uuid not null references public.providers(id) on delete cascade,
  name text not null,
  secret_version smallint not null,
  secret_nonce text not null,
  secret_ciphertext text not null,
  masked_hint text,
  enabled boolean not null default true,
  priority integer not null default 100,
  health public.gateway_health_state not null default 'healthy',
  quota_limit numeric(18, 8) check (quota_limit is null or quota_limit >= 0),
  quota_used numeric(18, 8) not null default 0 check (quota_used >= 0),
  quota_threshold numeric(5, 4) not null default 0.95 check (quota_threshold > 0 and quota_threshold <= 1),
  requests_per_minute integer check (requests_per_minute is null or requests_per_minute > 0),
  tokens_per_minute bigint check (tokens_per_minute is null or tokens_per_minute > 0),
  cooldown_until timestamptz,
  last_used_at timestamptz,
  last_success_at timestamptz,
  last_failure_at timestamptz,
  success_count bigint not null default 0,
  failure_count bigint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider_id, name)
);

create table public.models (
  id text primary key,
  display_name text not null,
  enabled boolean not null default true,
  capabilities text[] not null default '{}',
  context_window bigint,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.model_aliases (
  alias text primary key,
  model_id text not null references public.models(id) on delete cascade,
  created_at timestamptz not null default now()
);

create table public.provider_models (
  id uuid primary key default gen_random_uuid(),
  provider_id uuid not null references public.providers(id) on delete cascade,
  model_id text not null references public.models(id) on delete cascade,
  upstream_model_id text not null,
  protocol text not null,
  capabilities text[] not null default '{}',
  enabled boolean not null default true,
  priority integer not null default 100,
  weight numeric not null default 1 check (weight > 0),
  pricing jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider_id, model_id, upstream_model_id)
);

create table public.credential_model_access (
  credential_id uuid not null references public.provider_credentials(id) on delete cascade,
  provider_model_id uuid not null references public.provider_models(id) on delete cascade,
  primary key (credential_id, provider_model_id)
);

create table public.routing_policies (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  enabled boolean not null default true,
  policy jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.model_routes (
  id uuid primary key default gen_random_uuid(),
  model_id text not null references public.models(id) on delete cascade,
  provider_model_id uuid not null references public.provider_models(id) on delete cascade,
  policy_id uuid references public.routing_policies(id) on delete set null,
  priority integer not null default 100,
  enabled boolean not null default true,
  allow_model_fallback boolean not null default false,
  created_at timestamptz not null default now(),
  unique (model_id, provider_model_id)
);

create table public.config_versions (
  id bigint generated always as identity primary key,
  status text not null check (status in ('draft', 'published', 'superseded')),
  schema_version integer not null,
  payload jsonb not null,
  checksum text not null,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  published_at timestamptz
);

create unique index config_versions_one_published
  on public.config_versions ((status)) where status = 'published';

create table public.request_logs (
  id text primary key,
  client_id uuid references public.gateway_clients(id) on delete set null,
  protocol text not null,
  requested_model text not null,
  resolved_model text,
  status text not null,
  started_at timestamptz not null,
  ended_at timestamptz,
  latency_ms numeric,
  retry_count integer not null default 0,
  fallback_count integer not null default 0,
  error_category text,
  created_at timestamptz not null default now()
);

create table public.request_attempts (
  id bigint generated always as identity primary key,
  request_id text not null references public.request_logs(id) on delete cascade,
  attempt_number integer not null,
  provider_id uuid references public.providers(id) on delete set null,
  credential_id uuid references public.provider_credentials(id) on delete set null,
  provider_model_id uuid references public.provider_models(id) on delete set null,
  status text not null,
  upstream_status integer,
  error_category text,
  response_committed boolean not null default false,
  started_at timestamptz not null,
  ended_at timestamptz,
  latency_ms numeric,
  unique (request_id, attempt_number)
);

create table public.usage_records (
  id bigint generated always as identity primary key,
  request_id text not null references public.request_logs(id) on delete cascade,
  attempt_id bigint references public.request_attempts(id) on delete set null,
  input_tokens bigint,
  output_tokens bigint,
  cached_tokens bigint,
  estimated_cost numeric(18, 8),
  currency text not null default 'USD',
  is_estimate boolean not null default true,
  recorded_at timestamptz not null default now()
);

create table public.health_checks (
  id bigint generated always as identity primary key,
  provider_id uuid references public.providers(id) on delete cascade,
  credential_id uuid references public.provider_credentials(id) on delete cascade,
  status public.gateway_health_state not null,
  latency_ms numeric,
  error_category text,
  checked_at timestamptz not null default now(),
  check (provider_id is not null or credential_id is not null)
);

create table public.provider_events (
  id bigint generated always as identity primary key,
  provider_id uuid references public.providers(id) on delete cascade,
  credential_id uuid references public.provider_credentials(id) on delete cascade,
  event_type text not null,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table public.audit_logs (
  id bigint generated always as identity primary key,
  actor_id uuid references auth.users(id) on delete set null,
  action text not null,
  resource_type text not null,
  resource_id text,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table public.system_settings (
  key text primary key,
  value jsonb not null,
  updated_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now()
);

create index gateway_client_keys_client_idx on public.gateway_client_keys(client_id);
create index credentials_provider_health_idx on public.provider_credentials(provider_id, enabled, health);
create index provider_models_route_idx on public.provider_models(model_id, enabled, priority);
create index request_logs_time_idx on public.request_logs(started_at desc);
create index request_logs_client_time_idx on public.request_logs(client_id, started_at desc);
create index request_logs_status_time_idx on public.request_logs(status, started_at desc);
create index request_attempts_request_idx on public.request_attempts(request_id, attempt_number);
create index usage_records_time_idx on public.usage_records(recorded_at desc);
create index health_checks_provider_time_idx on public.health_checks(provider_id, checked_at desc);
create index provider_events_time_idx on public.provider_events(created_at desc);
create index audit_logs_time_idx on public.audit_logs(created_at desc);

alter table public.gateway_clients enable row level security;
alter table public.gateway_client_keys enable row level security;
alter table public.providers enable row level security;
alter table public.provider_credentials enable row level security;
alter table public.models enable row level security;
alter table public.model_aliases enable row level security;
alter table public.provider_models enable row level security;
alter table public.credential_model_access enable row level security;
alter table public.routing_policies enable row level security;
alter table public.model_routes enable row level security;
alter table public.config_versions enable row level security;
alter table public.request_logs enable row level security;
alter table public.request_attempts enable row level security;
alter table public.usage_records enable row level security;
alter table public.health_checks enable row level security;
alter table public.provider_events enable row level security;
alter table public.audit_logs enable row level security;
alter table public.system_settings enable row level security;

comment on table public.provider_credentials is
  'Ciphertext only. Plaintext provider secrets must never be stored in PostgreSQL.';
comment on table public.config_versions is
  'Immutable validated data-plane snapshots; exactly one version may be published.';

commit;
