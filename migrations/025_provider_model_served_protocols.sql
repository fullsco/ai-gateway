-- Let a mapping answer client protocols its upstream does not speak.
--
-- Route selection required the client protocol and the upstream protocol to be the
-- same value, so a model reachable only over Chat Completions was unreachable from
-- any Anthropic Messages client. Claude Code speaks only `/v1/messages`, so glm-5.3,
-- gpt-5.6-sol, deepseek-v4-flash, kimi-k3 and the rest of the OpenAI-protocol
-- mappings answered 404 model_unavailable no matter what the client configured. The
-- same wall stopped OpenAI-protocol clients from reaching Anthropic-only upstreams.
--
-- `protocol` keeps its meaning: the protocol the gateway speaks *to the provider*.
-- The uniqueness constraint from 007 is over that column and is untouched, so a
-- provider still has one mapping per (model, upstream model, upstream protocol).
-- `serves_protocols` is the separate question of which client APIs that mapping
-- answers, with the gateway translating request, response and SSE stream when the
-- two differ.
--
-- Existing rows are backfilled to their own protocol, which is exactly today's
-- behaviour, so applying this migration changes no routing decision on its own.

begin;

alter table public.provider_models
  add column if not exists serves_protocols text[] not null default '{}'::text[];

update public.provider_models
set serves_protocols = array[protocol]
where serves_protocols = '{}'::text[];

-- Same three-value vocabulary as providers.protocol and provider_models.protocol
-- (003_protocol_constraints.sql). An empty array is permitted and means "the upstream
-- protocol only": a writer that does not know about this column yet produces a row
-- that behaves exactly as it did before, rather than a row that serves nothing.
alter table public.provider_models
  add constraint provider_models_serves_protocols_valid
  check (
    serves_protocols <@ array[
      'anthropic_messages',
      'openai_chat_completions',
      'openai_responses'
    ]::text[]
  );

comment on column public.provider_models.serves_protocols is
  'Client APIs this mapping answers. Entries other than `protocol` are served by '
  'translating the request, response and SSE stream. Empty means the upstream '
  'protocol only, which is how every row behaved before this column existed.';

commit;
