begin;

do $$
declare
  anthropic_id uuid;
  openai_id uuid;
begin
  select id into anthropic_id from public.providers where name = 'AgentRouter Anthropic';
  select id into openai_id from public.providers where name = 'AgentRouter OpenAI';

  if anthropic_id is null or openai_id is null then
    raise exception 'Expected both AgentRouter provider records before consolidation';
  end if;

  if anthropic_id = openai_id then
    raise exception 'AgentRouter provider records must be distinct before consolidation';
  end if;

  insert into public.credential_model_access(credential_id, provider_model_id)
  select c.id, pm.id
  from public.provider_credentials c
  join public.provider_models pm on pm.provider_id = c.provider_id
  where c.provider_id in (anthropic_id, openai_id)
  on conflict do nothing;

  update public.provider_models pm
  set settings = p.settings,
      provider_id = anthropic_id,
      updated_at = now()
  from public.providers p
  where pm.provider_id = p.id
    and p.id in (anthropic_id, openai_id);

  update public.provider_credentials
  set provider_id = anthropic_id,
      updated_at = now()
  where provider_id = openai_id;

  update public.providers
  set name = 'AgentRouter',
      provider_type = null,
      protocol = null,
      updated_at = now()
  where id = anthropic_id;

  update public.providers
  set name = 'AgentRouter OpenAI (archived)',
      enabled = false,
      updated_at = now()
  where id = openai_id;
end $$;

commit;
