begin;

create index audit_logs_actor_idx on public.audit_logs(actor_id);
create index config_versions_created_by_idx on public.config_versions(created_by);
create index credential_model_access_provider_model_idx
  on public.credential_model_access(provider_model_id);
create index health_checks_credential_time_idx
  on public.health_checks(credential_id, checked_at desc);
create index model_aliases_model_idx on public.model_aliases(model_id);
create index model_routes_policy_idx on public.model_routes(policy_id);
create index model_routes_provider_model_idx on public.model_routes(provider_model_id);
create index provider_events_credential_time_idx
  on public.provider_events(credential_id, created_at desc);
create index provider_events_provider_time_idx
  on public.provider_events(provider_id, created_at desc);
create index request_attempts_credential_idx on public.request_attempts(credential_id);
create index request_attempts_provider_idx on public.request_attempts(provider_id);
create index request_attempts_provider_model_idx on public.request_attempts(provider_model_id);
create index system_settings_updated_by_idx on public.system_settings(updated_by);
create index usage_records_attempt_idx on public.usage_records(attempt_id);
create index usage_records_request_idx on public.usage_records(request_id);

create policy deny_direct_access on public.gateway_clients for all to public
  using (false) with check (false);
create policy deny_direct_access on public.gateway_client_keys for all to public
  using (false) with check (false);
create policy deny_direct_access on public.providers for all to public
  using (false) with check (false);
create policy deny_direct_access on public.provider_credentials for all to public
  using (false) with check (false);
create policy deny_direct_access on public.models for all to public
  using (false) with check (false);
create policy deny_direct_access on public.model_aliases for all to public
  using (false) with check (false);
create policy deny_direct_access on public.provider_models for all to public
  using (false) with check (false);
create policy deny_direct_access on public.credential_model_access for all to public
  using (false) with check (false);
create policy deny_direct_access on public.routing_policies for all to public
  using (false) with check (false);
create policy deny_direct_access on public.model_routes for all to public
  using (false) with check (false);
create policy deny_direct_access on public.config_versions for all to public
  using (false) with check (false);
create policy deny_direct_access on public.request_logs for all to public
  using (false) with check (false);
create policy deny_direct_access on public.request_attempts for all to public
  using (false) with check (false);
create policy deny_direct_access on public.usage_records for all to public
  using (false) with check (false);
create policy deny_direct_access on public.health_checks for all to public
  using (false) with check (false);
create policy deny_direct_access on public.provider_events for all to public
  using (false) with check (false);
create policy deny_direct_access on public.audit_logs for all to public
  using (false) with check (false);
create policy deny_direct_access on public.system_settings for all to public
  using (false) with check (false);

commit;
