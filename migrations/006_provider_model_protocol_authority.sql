begin;

alter table public.providers
  alter column provider_type drop not null,
  alter column protocol drop not null;

comment on column public.providers.protocol is
  'Legacy compatibility metadata. Provider-model protocol is authoritative.';
comment on column public.providers.provider_type is
  'Legacy compatibility metadata. Adapter family is derived from provider-model protocol.';

commit;
