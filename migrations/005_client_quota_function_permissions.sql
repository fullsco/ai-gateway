begin;

revoke execute on function public.reserve_client_quota(uuid, integer, bigint, bigint)
  from public, anon, authenticated;
grant execute on function public.reserve_client_quota(uuid, integer, bigint, bigint)
  to service_role;

commit;
