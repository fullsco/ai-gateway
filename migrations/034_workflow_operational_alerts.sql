-- Expand-only condition vocabulary. No rules or notification destinations are seeded.
begin;

alter table public.alert_rules drop constraint if exists alert_rules_condition_kind_valid;
alter table public.alert_rules add constraint alert_rules_condition_kind_valid check (
  condition_kind is null or condition_kind in (
    'budget_utilization', 'credential_quota_low', 'credential_balance_low',
    'credential_auth_failures', 'provider_failure_rate', 'provider_unreachable',
    'model_no_eligible_route', 'credential_pool_exhausted', 'request_failure_rate',
    'cost_spike', 'unpriced_traffic', 'workflow_queue_age', 'settlement_backlog',
    'unknown_usage', 'workflow_approval_backlog'
  )
);

commit;
