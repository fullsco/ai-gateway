begin;

create or replace function public.set_alert_delivery_sequence() returns trigger
language plpgsql set search_path = pg_catalog as $$
declare
  subscribed boolean;
begin
  select (delivery_destination is not null) into subscribed
    from public.alert_rules where id = NEW.rule_id;
  if not coalesce(subscribed, false) then
    NEW.delivery_sequence := 0;
  elsif TG_OP = 'INSERT' then
    NEW.delivery_sequence := case when NEW.status = 'open' then 1 else 0 end;
  elsif NEW.status is distinct from OLD.status
        and NEW.status in ('open','resolved') then
    NEW.delivery_sequence := OLD.delivery_sequence + 1;
  else
    NEW.delivery_sequence := OLD.delivery_sequence;
  end if;
  return NEW;
end;
$$;

revoke all on function public.set_alert_delivery_sequence()
  from public, anon, authenticated, service_role;

commit;
