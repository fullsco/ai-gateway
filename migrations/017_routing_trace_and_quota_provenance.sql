-- Routing explainability + dynamic operational state support.
--
-- 1. request_logs.routing_trace records, per request, which provider/credential
--    candidates were considered, which were excluded and why, which was selected,
--    and whether a fallback occurred. This makes routing decisions auditable.
-- 2. provider_credentials.quota_source records where quota_used came from, so the
--    router can distinguish known measurements from unknown/estimated values and
--    never treat a guess as a fact.

begin;

alter table public.request_logs
  add column if not exists routing_trace jsonb;

comment on column public.request_logs.routing_trace is
  'Per-attempt routing decision trace: candidates considered, exclusion reasons, selection, fallback.';

do $$
begin
  if not exists (select 1 from pg_type where typname = 'gateway_quota_source') then
    create type public.gateway_quota_source as enum ('unknown', 'operator', 'upstream_usage');
  end if;
end $$;

alter table public.provider_credentials
  add column if not exists quota_source public.gateway_quota_source not null default 'unknown',
  add column if not exists quota_observed_at timestamptz,
  add column if not exists quota_note text;

comment on column public.provider_credentials.quota_source is
  'Provenance of quota_used: unknown (no signal), operator (manually entered), upstream_usage (polled from the provider).';
comment on column public.provider_credentials.quota_observed_at is
  'When quota_used was last refreshed from its source.';

commit;
