-- Record why a credential is parked, and let an operator put it back in service.
--
-- Credential health only ever moved on its own, driven by observed successes and
-- failures, and there was nowhere to write down what an operator learned about a
-- key. That left two gaps.
--
-- The first is that a key parked as auth_failed could not be returned to service
-- once it was fixed. Health is restored by observing a success, and an unhealthy
-- credential is not selected, so nothing could break the cycle except waiting for
-- a cooldown that in these cases was never set.
--
-- The second is that "auth_failed" was carrying the weight of four different
-- provider answers. Probing the eight parked AgentRouter credentials directly, with
-- the mapping's real headers, found four that work and four that fail for reasons
-- that are not authentication at all:
--
--   restored-1eaf91d1  works, and had never recorded a single success
--   restored-d4fbedbd  works
--   restored-1bfe41ca  works, and was disabled
--   restored-d37159cf  works
--   restored-949513d4  403 user quota is not enough
--   restored-3d5cd725  403 user quota is not enough
--   restored-f025f8ba  403 the caller's IP is not on the token's allow list
--   restored-5acd2785  403 the token may not access claude-opus-5
--
-- Only the last two need a human. The note column is where that distinction lives,
-- because the health enum cannot express it and the operator has no other place to
-- leave a finding for whoever looks next.

alter table public.provider_credentials
  add column if not exists note text;

comment on column public.provider_credentials.note is
  'Operator findings about this credential: why it is parked, what was probed, and '
  'what would fix it. Free text, shown in the Credentials view. Not written by the '
  'gateway, which only maintains health, cooldown and counters.';
