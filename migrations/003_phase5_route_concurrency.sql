begin;

alter table public.provider_models
  add column max_concurrency integer not null default 8
  check (max_concurrency > 0);

commit;
