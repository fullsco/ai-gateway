begin;

alter function public.reject_usage_record_update() set search_path = '';

create index usage_records_attempt_request_idx
  on public.usage_records(attempt_id, request_id);

commit;
