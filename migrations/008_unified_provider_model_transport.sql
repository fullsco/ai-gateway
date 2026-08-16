begin;

alter table public.provider_models
  add column if not exists settings jsonb not null default '{}';

comment on column public.provider_models.settings is
  'Protocol-specific transport settings. Overrides provider defaults for this mapping.';

commit;
