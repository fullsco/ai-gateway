-- Operable alerting.
--
-- The machinery existed but could not express an operational condition. Rules
-- matched an event type and compared metadata for exact equality, so "failure
-- rate above 50%" or "five auth failures in ten minutes" were inexpressible, and
-- authoring a rule meant knowing internal event names and writing raw JSON.
-- Nothing ever resolved an alert either: resolved_at existed and no code set it,
-- so every alert stayed open forever and the operator could not tell a live
-- problem from a historical one.
--
-- condition_kind names a condition the monitor knows how to evaluate, with its
-- thresholds in condition. The prose columns travel with the alert so the
-- operator is told what happened, why it matters and what to do about it without
-- reading the schema.

begin;

alter table public.alert_rules
  add column if not exists condition_kind text,
  add column if not exists description text,
  add column if not exists impact text,
  add column if not exists recommended_action text;

-- event_type is only meaningful for rules driven by a request-time event. A rule
-- evaluated by the monitor is identified by condition_kind instead.
alter table public.alert_rules alter column event_type drop not null;

alter table public.alert_rules
  drop constraint if exists alert_rules_condition_kind_valid;
alter table public.alert_rules
  add constraint alert_rules_condition_kind_valid check (
    condition_kind is null or condition_kind in (
      'credential_quota_low',
      'credential_balance_low',
      'credential_auth_failures',
      'provider_failure_rate',
      'provider_unreachable',
      'model_no_eligible_route',
      'credential_pool_exhausted',
      'request_failure_rate',
      'cost_spike',
      'unpriced_traffic'
    )
  );

alter table public.alert_rules
  drop constraint if exists alert_rules_has_a_trigger;
alter table public.alert_rules
  add constraint alert_rules_has_a_trigger check (
    event_type is not null or condition_kind is not null
  );

comment on column public.alert_rules.condition_kind is
  'Named operational condition the alert monitor evaluates. Null means the rule fires from a request-time event instead.';
comment on column public.alert_rules.impact is
  'Why this condition matters, in the operator''s terms.';
comment on column public.alert_rules.recommended_action is
  'What the operator should do. Blank means observe only.';

alter table public.alerts
  add column if not exists summary text,
  add column if not exists impact text,
  add column if not exists recommended_action text,
  add column if not exists resolved_reason text,
  add column if not exists observed jsonb;

comment on column public.alerts.observed is
  'The measured values that satisfied the condition, so the alert can be judged without re-querying.';
comment on column public.alerts.resolved_reason is
  'Why the alert closed: recovered when the condition cleared, or resolved by an operator.';

create index if not exists alerts_scope_idx on public.alerts(scope_type, scope_id, status);

commit;
