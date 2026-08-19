-- Cost measurement provenance.
--
-- Cost was previously a single nullable estimate with a hardcoded is_estimate
-- flag, so four genuinely different facts were indistinguishable:
--
--   1. settled cost      - what the provider actually charged, measured
--   2. usage measurement - provider-reported token counts
--   3. preauthorization  - the hold a provider requires before inference
--   4. estimated cost    - pricing configuration multiplied by tokens
--
-- cost_samples retains every independent measurement, including the raw
-- before/after counter readings, so a blended rate is derived from evidence and
-- sharpens as samples accumulate rather than being asserted once.
--
-- provider_preauth_requirements records what a provider demands up front. A
-- request can be rejected before inference for want of balance while costing
-- nothing, which is a different condition from being over budget.
--
-- provider_credentials gains explicit balance columns. Balance is money
-- remaining, which is not the same as quota_used (money spent), and overloading
-- the quota columns would have made the router treat a balance as a ceiling.

begin;

create table if not exists public.cost_samples (
  id bigint generated always as identity primary key,
  provider_id uuid not null references public.providers(id) on delete cascade,
  provider_name_snapshot text not null,
  model_id text,
  upstream_model text,
  measured_at timestamptz not null default now(),
  method text not null check (method in (
    'billing_usage_delta', 'provider_reported', 'invoice', 'operator_entered'
  )),
  usage_before numeric(24,8),
  usage_after numeric(24,8),
  raw_delta numeric(24,8),
  scale text not null default 'unknown' check (scale in ('unknown', 'cent', 'unit')),
  measured_cost numeric(18,8) check (measured_cost is null or measured_cost >= 0),
  currency text check (currency is null or length(trim(currency)) = 3),
  input_tokens integer check (input_tokens is null or input_tokens >= 0),
  output_tokens integer check (output_tokens is null or output_tokens >= 0),
  cached_tokens integer check (cached_tokens is null or cached_tokens >= 0),
  request_id text,
  note text,
  created_at timestamptz not null default now(),
  -- A measured cost is meaningless without the currency it is denominated in.
  constraint cost_samples_currency_present check (
    measured_cost is null or currency is not null
  )
);

comment on table public.cost_samples is
  'Independent cost measurements. Raw before/after readings are retained so derived rates improve as samples accumulate.';
comment on column public.cost_samples.scale is
  'Unit convention of raw_delta: cent means raw_delta/100 is the currency amount; unit means it already is.';
comment on column public.cost_samples.method is
  'How the sample was obtained. billing_usage_delta is a before/after read of the provider usage counter around one request.';

create index if not exists cost_samples_provider_model_idx
  on public.cost_samples(provider_id, model_id, measured_at desc);

create table if not exists public.provider_preauth_requirements (
  provider_id uuid not null references public.providers(id) on delete cascade,
  model_id text not null,
  required_amount numeric(18,8) not null check (required_amount >= 0),
  currency text not null check (length(trim(currency)) = 3),
  observed_at timestamptz not null default now(),
  source text not null default 'observed' check (source in ('observed', 'documented')),
  note text,
  primary key (provider_id, model_id)
);

comment on table public.provider_preauth_requirements is
  'Balance a provider requires before it will run inference. A request short of it is rejected without being billed.';

alter table public.provider_credentials
  add column if not exists balance_amount numeric(18,8),
  add column if not exists balance_currency text,
  add column if not exists balance_observed_at timestamptz,
  add column if not exists balance_source text;

alter table public.provider_credentials
  drop constraint if exists provider_credentials_balance_source_valid;
alter table public.provider_credentials
  add constraint provider_credentials_balance_source_valid check (
    balance_source is null or balance_source in ('operator', 'upstream_balance')
  );

comment on column public.provider_credentials.balance_amount is
  'Money remaining on the credential. Distinct from quota_used, which is money already spent.';

-- Derived, never authoritative on its own: exposes sample count and confidence
-- alongside the rate so a single measurement is never presented as a fact.
create or replace view public.provider_cost_profile as
select
  s.provider_id,
  s.provider_name_snapshot,
  s.model_id,
  s.currency,
  count(*) as sample_count,
  min(s.measured_at) as first_measured_at,
  max(s.measured_at) as last_measured_at,
  sum(s.measured_cost) as measured_cost_total,
  sum(coalesce(s.input_tokens, 0)) as input_tokens_total,
  sum(coalesce(s.output_tokens, 0)) as output_tokens_total,
  sum(coalesce(s.input_tokens, 0) + coalesce(s.output_tokens, 0)) as total_tokens,
  case
    when sum(coalesce(s.input_tokens, 0) + coalesce(s.output_tokens, 0)) > 0
    then round(
      sum(s.measured_cost)
      / sum(coalesce(s.input_tokens, 0) + coalesce(s.output_tokens, 0))
      * 1000000, 6)
  end as blended_per_million,
  case
    when count(*) >= 20 then 'high'
    when count(*) >= 5 then 'medium'
    else 'low'
  end as confidence
from public.cost_samples s
where s.measured_cost is not null
group by s.provider_id, s.provider_name_snapshot, s.model_id, s.currency;

comment on view public.provider_cost_profile is
  'Blended cost per million tokens derived from cost_samples, with sample count and confidence. Low confidence means do not extrapolate.';

alter table public.cost_samples enable row level security;
alter table public.provider_preauth_requirements enable row level security;
create policy deny_direct_access on public.cost_samples
  for all to public using (false) with check (false);
create policy deny_direct_access on public.provider_preauth_requirements
  for all to public using (false) with check (false);

commit;
