import math
import re
from numbers import Real
from uuid import UUID

_DESTINATION = re.compile(r"[a-zA-Z0-9_-]{1,64}", re.ASCII)
_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)

_SCHEMA_SQL = """
select not exists (
  select 1 from (values
    ('alert_rules','delivery_destination','text'),
    ('alerts','delivery_sequence','bigint'),
    ('alert_delivery_outbox','id','uuid'),
    ('alert_delivery_outbox','alert_id','bigint'),
    ('alert_delivery_outbox','destination','text'),
    ('alert_delivery_outbox','sequence','bigint'),
    ('alert_delivery_outbox','event_type','text'),
    ('alert_delivery_outbox','severity','text'),
    ('alert_delivery_outbox','status','text'),
    ('alert_delivery_outbox','previous_status','text'),
    ('alert_delivery_outbox','occurred_at','timestamp with time zone'),
    ('alert_delivery_outbox','delivery_status','text'),
    ('alert_delivery_outbox','attempts','integer'),
    ('alert_delivery_outbox','next_attempt_at','timestamp with time zone'),
    ('alert_delivery_outbox','lease_token','uuid'),
    ('alert_delivery_outbox','lease_until','timestamp with time zone'),
    ('alert_delivery_outbox','last_error','text'),
    ('alert_delivery_outbox','last_status','integer')
  ) expected(table_name,column_name,data_type)
  where not exists (
    select 1 from information_schema.columns actual
    where actual.table_schema='public' and actual.table_name=expected.table_name
      and actual.column_name=expected.column_name and actual.data_type=expected.data_type
  )
) and (
  select count(*)=2 from pg_catalog.pg_trigger
  where tgrelid=to_regclass('public.alerts') and not tgisinternal
    and tgenabled in ('O','A')
    and tgname in ('alert_delivery_sequence','alert_delivery_capture')
) as ready
"""

_CLAIM_SQL = """
with expired as (
  select id from public.alert_delivery_outbox
  where destination=$1 and delivery_status='pending'
    and (lease_until is null or lease_until<=now())
    and (attempts >= $2 or occurred_at<=now()-$4::double precision*interval '1 second')
  order by occurred_at,id
  for update skip locked limit 100
), retired as (
  update public.alert_delivery_outbox o
  set delivery_status='dead',lease_token=null,lease_until=null,
    last_error=case when o.attempts >= $2 then 'max_attempts' else 'max_age' end
  from expired where o.id=expired.id
  returning o.id
), candidate as (
  select id from public.alert_delivery_outbox
  where destination=$1 and delivery_status='pending'
    and (lease_until is null or lease_until<=now())
    and next_attempt_at<=now() and attempts < $2
    and occurred_at>now()-$4::double precision*interval '1 second'
    and (select count(*) from retired)>=0
  order by occurred_at,id
  for update skip locked limit 1
)
update public.alert_delivery_outbox o
set attempts=o.attempts+1,lease_token=gen_random_uuid(),
  lease_until=now()+$3::double precision*interval '1 second'
from candidate where o.id=candidate.id
returning o.*
"""

_FINISH_SQL = """
update public.alert_delivery_outbox
set delivery_status=$3,
  next_attempt_at=case when $3='pending'
    then now()+$4::double precision*interval '1 second' else next_attempt_at end,
  last_error=$5,last_status=$6,lease_token=null,lease_until=null
where id=$1 and lease_token=$2 and lease_until>now() and delivery_status='pending'
returning id
"""


def _seconds(value, name, maximum, *, allow_zero=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or value < 0
        or (value == 0 and not allow_zero)
        or value > maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"Invalid {name}")
    return float(value)


def _uuid(value, name):
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ValueError(f"Invalid {name}")


class AlertDeliveryStore:
    def __init__(self, pool):
        self.pool = pool

    async def require_schema(self) -> None:
        row = await self.pool.fetchrow(_SCHEMA_SQL)
        if row is None or not row["ready"]:
            raise RuntimeError("Alert delivery schema migration 035 is required")

    async def claim(
        self, destination, *, max_attempts, lease_seconds, max_age_seconds,
    ) -> dict | None:
        if not isinstance(destination, str) or not _DESTINATION.fullmatch(destination):
            raise ValueError("Invalid destination")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 2147483647
        ):
            raise ValueError("Invalid max_attempts")
        lease_seconds = _seconds(lease_seconds, "lease_seconds", 86400)
        max_age_seconds = _seconds(max_age_seconds, "max_age_seconds", 31536000)
        row = await self.pool.fetchrow(
            _CLAIM_SQL, destination, max_attempts, lease_seconds, max_age_seconds,
        )
        return dict(row) if row is not None else None

    async def finish(
        self, event_id, token, *, outcome, delay_seconds=0, error=None, status_code=None,
    ) -> bool:
        event_id = _uuid(event_id, "event_id")
        token = _uuid(token, "token")
        if not isinstance(outcome, str) or outcome not in {"delivered", "pending", "dead"}:
            raise ValueError("Invalid outcome")
        delay_seconds = _seconds(delay_seconds, "delay_seconds", 31536000, allow_zero=True)
        if error is not None and (not isinstance(error, str) or not _ERROR.fullmatch(error)):
            raise ValueError("Invalid error category")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise ValueError("Invalid status_code")
        row = await self.pool.fetchrow(
            _FINISH_SQL, event_id, token, outcome, delay_seconds, error, status_code,
        )
        return row is not None
