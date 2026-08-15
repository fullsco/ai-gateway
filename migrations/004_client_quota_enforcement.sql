begin;

create table public.client_quota_windows (
  client_id uuid not null references public.gateway_clients(id) on delete cascade,
  window_started_at timestamptz not null,
  request_count integer not null default 0 check (request_count >= 0),
  token_count bigint not null default 0 check (token_count >= 0),
  primary key (client_id, window_started_at)
);

create index client_quota_windows_expiry_idx
  on public.client_quota_windows(window_started_at);

create or replace function public.reserve_client_quota(
  p_client_id uuid,
  p_requests_per_minute integer,
  p_tokens_per_minute bigint,
  p_estimated_tokens bigint
) returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  current_window timestamptz := date_trunc('minute', now());
  current_requests integer;
  current_tokens bigint;
begin
  if p_estimated_tokens < 0 then
    return 'tokens';
  end if;

  insert into client_quota_windows(client_id, window_started_at)
  values (p_client_id, current_window)
  on conflict (client_id, window_started_at) do nothing;

  select request_count, token_count
    into current_requests, current_tokens
    from client_quota_windows
   where client_id = p_client_id and window_started_at = current_window
   for update;

  if p_requests_per_minute is not null
     and current_requests + 1 > p_requests_per_minute then
    return 'requests';
  end if;
  if p_tokens_per_minute is not null
     and current_tokens + p_estimated_tokens > p_tokens_per_minute then
    return 'tokens';
  end if;

  update client_quota_windows
     set request_count = request_count + 1,
         token_count = token_count + p_estimated_tokens
   where client_id = p_client_id and window_started_at = current_window;
  return null;
end;
$$;

revoke all on function public.reserve_client_quota(uuid, integer, bigint, bigint)
  from public, anon, authenticated;
grant execute on function public.reserve_client_quota(uuid, integer, bigint, bigint)
  to service_role;

alter table public.client_quota_windows enable row level security;
create policy deny_direct_access on public.client_quota_windows for all to public
  using (false) with check (false);

commit;
