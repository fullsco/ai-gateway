begin;

update public.gateway_clients
set allowed_protocols = array_replace(
  allowed_protocols,
  'openai_chat',
  'openai_chat_completions'
), updated_at = now()
where 'openai_chat' = any(allowed_protocols);

alter table public.gateway_clients
  add constraint gateway_clients_allowed_protocols_valid
  check (
    allowed_protocols <@ array[
      'anthropic_messages',
      'openai_chat_completions',
      'openai_responses'
    ]::text[]
  );

alter table public.providers
  add constraint providers_protocol_valid
  check (protocol in ('anthropic_messages', 'openai_chat_completions', 'openai_responses'));

alter table public.provider_models
  add constraint provider_models_protocol_valid
  check (protocol in ('anthropic_messages', 'openai_chat_completions', 'openai_responses'));

commit;
