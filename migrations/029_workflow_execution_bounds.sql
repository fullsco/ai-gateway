alter table public.workflow_jobs drop constraint workflow_jobs_max_attempts_check;
alter table public.workflow_jobs add constraint workflow_jobs_max_attempts_check
  check (max_attempts between 1 and 128);
alter table public.workflow_jobs add column reserved_tokens bigint not null default 0
  check (reserved_tokens >= 0);
alter table public.workflow_jobs add column reserved_cost numeric(18,8) not null default 0
  check (reserved_cost >= 0);
alter table public.financial_reservations add column workflow_id uuid
  references public.workflow_jobs(id);
create index financial_reservations_workflow_idx on public.financial_reservations(workflow_id);
