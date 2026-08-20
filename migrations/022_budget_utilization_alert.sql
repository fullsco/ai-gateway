-- Allow the budget utilization alert condition.
--
-- gateway_budgets.warning_threshold was stored, editable and displayed but never
-- evaluated by anything, so a blocking budget could only announce itself by
-- refusing traffic. This condition lets a rule warn while there is still headroom.

begin;

alter table public.alert_rules
  drop constraint if exists alert_rules_condition_kind_valid;
alter table public.alert_rules
  add constraint alert_rules_condition_kind_valid check (
    condition_kind is null or condition_kind in (
      'budget_utilization',
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

commit;
