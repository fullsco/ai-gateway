begin;

alter table public.provider_models
  drop constraint provider_models_provider_id_model_id_upstream_model_id_key,
  add constraint provider_models_provider_model_protocol_key
    unique (provider_id, model_id, upstream_model_id, protocol);

commit;
