-- Cache writes are billed at a premium and must be priced as such.
--
-- Anthropic reports three input dimensions: fresh tokens, tokens read from the
-- prompt cache, and tokens written to it. The write is charged above the base
-- input rate. Folding writes into the input total priced them at the base rate,
-- which understated real traffic by 14.5%: reconciling gateway cost against the
-- provider's own billing counter showed $1.4648 recorded against $1.7130 billed,
-- and pricing the write portion at the measured premium reproduces the billed
-- figure to within 0.001%. Claude Code caches its context on almost every turn,
-- so nearly all of its non-cached input is a cache write.

begin;

alter table public.usage_records
  add column if not exists cache_write_tokens integer
    check (cache_write_tokens is null or cache_write_tokens >= 0);

comment on column public.usage_records.cache_write_tokens is
  'Input tokens written to the provider prompt cache. Billed above the base input rate.';

commit;
