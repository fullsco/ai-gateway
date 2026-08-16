begin;

alter table public.request_attempts
  add constraint request_attempts_id_request_unique unique (id, request_id);

alter table public.usage_records
  add column provider_id_snapshot uuid,
  add column provider_name_snapshot text,
  add column provider_model_id_snapshot uuid,
  add column route_id_snapshot uuid,
  add column canonical_model_snapshot text,
  add column upstream_model_snapshot text,
  add column protocol_snapshot text,
  add column attempt_status_snapshot text,
  add column pricing_context jsonb,
  add column pricing_context_hash text;

alter table public.usage_records
  alter column currency drop not null,
  alter column currency drop default;

update public.usage_records u set
  provider_id_snapshot = a.provider_id,
  provider_name_snapshot = p.name,
  provider_model_id_snapshot = a.provider_model_id,
  route_id_snapshot = (
    select mr.id from public.model_routes mr
    where mr.provider_model_id = a.provider_model_id
      and mr.model_id = coalesce(pm.model_id, r.resolved_model, r.requested_model)
    order by mr.created_at limit 1
  ),
  canonical_model_snapshot = coalesce(pm.model_id, r.resolved_model, r.requested_model),
  upstream_model_snapshot = pm.upstream_model_id,
  protocol_snapshot = coalesce(pm.protocol, r.protocol),
  attempt_status_snapshot = a.status,
  pricing_context = case when u.estimated_cost is not null
    then jsonb_build_object(
      'legacy_backfill', true,
      'estimated_cost_preserved', true,
      'original_currency', u.currency
    ) else null end,
  pricing_context_hash = case when u.estimated_cost is not null
    then encode(digest(jsonb_build_object(
      'legacy_backfill', true,
      'estimated_cost_preserved', true,
      'original_currency', u.currency
    )::text, 'sha256'), 'hex') else null end
from public.request_attempts a
join public.request_logs r on r.id = a.request_id
left join public.providers p on p.id = a.provider_id
left join public.provider_models pm on pm.id = a.provider_model_id
where u.attempt_id = a.id;

update public.usage_records set currency = null where estimated_cost is null;

delete from public.usage_records duplicate
using public.usage_records retained
where duplicate.attempt_id is not null
  and duplicate.attempt_id = retained.attempt_id
  and duplicate.id > retained.id;

alter table public.usage_records
  drop constraint usage_records_attempt_id_fkey,
  alter column attempt_id set not null,
  add constraint usage_records_attempt_request_fkey
    foreign key (attempt_id, request_id)
    references public.request_attempts(id, request_id) on delete cascade,
  add constraint usage_records_input_tokens_nonnegative
    check (input_tokens is null or input_tokens >= 0),
  add constraint usage_records_output_tokens_nonnegative
    check (output_tokens is null or output_tokens >= 0),
  add constraint usage_records_cached_tokens_nonnegative
    check (cached_tokens is null or cached_tokens >= 0),
  add constraint usage_records_estimated_cost_nonnegative
    check (estimated_cost is null or estimated_cost >= 0),
  add constraint usage_records_pricing_context_consistent check (
    (estimated_cost is null and currency is null)
    or
    (estimated_cost is not null and currency is not null
      and length(trim(currency)) = 3
      and pricing_context is not null
      and pricing_context_hash is not null)
  );

create unique index usage_records_one_per_attempt
  on public.usage_records(attempt_id);
create index usage_records_provider_snapshot_time_idx
  on public.usage_records(provider_id_snapshot, recorded_at desc);
create index usage_records_model_snapshot_time_idx
  on public.usage_records(canonical_model_snapshot, recorded_at desc);
create index usage_records_route_snapshot_time_idx
  on public.usage_records(route_id_snapshot, recorded_at desc);
create index usage_records_currency_time_idx
  on public.usage_records(currency, recorded_at desc) where estimated_cost is not null;

comment on table public.usage_records is
  'At most one immutable usage and pricing attribution record per upstream attempt.';
comment on column public.usage_records.pricing_context is
  'Exact provider-model pricing object used when estimated_cost was calculated.';
comment on column public.usage_records.pricing_context_hash is
  'SHA-256 identifier for the canonical pricing context used at persistence time.';

create function public.reject_usage_record_update()
returns trigger language plpgsql as $$
begin
  raise exception 'usage records are immutable';
end;
$$;

create trigger usage_records_immutable
before update on public.usage_records
for each row execute function public.reject_usage_record_update();

commit;
