begin;

update public.providers
set provider_type = null,
    protocol = null,
    updated_at = now()
where id = '5a652733-cf5a-4b26-8fe5-e11b4820cf98'
  and name = 'AgentRouter';

commit;
