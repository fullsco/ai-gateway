"""Tenant-scoped durable jobs with fenced transitions and transactional event emission."""

import hashlib
import json
from uuid import uuid4


class WorkflowConflict(ValueError):
    pass


class LeaseLost(WorkflowConflict):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def unpack(row):
    if row is None:
        return None
    data = dict(row)
    for key in ("input", "checkpoint", "result", "data"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


class WorkflowStore:
    def __init__(self, pool, *, per_client_capacity=1, max_pending=32):
        self.pool = pool
        self.per_client_capacity = per_client_capacity
        self.max_pending = max_pending

    async def submit(self, client_id, key, payload, deadline_seconds=3600):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError("Idempotency-Key must be 1–200 characters")
        if len(encode(payload).encode()) > 256 * 1024:
            raise ValueError("Workflow input exceeds 256 KiB")
        if not 1 <= deadline_seconds <= 86400:
            raise ValueError("Workflow deadline must be 1–86400 seconds")
        checksum = digest(payload)
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval(
                "select id from public.gateway_clients where id=$1 for update", client_id,
            )
            replay = await connection.fetchrow(
                "select * from public.workflow_jobs where client_id=$1 and idempotency_key=$2",
                client_id, key,
            )
            if replay:
                if replay["input_hash"] != checksum:
                    raise WorkflowConflict("Idempotency key already used with different input")
                return unpack(replay)
            pending = await connection.fetchval(
                "select count(*) from public.workflow_jobs where client_id=$1 "
                "and status in ('queued','running','waiting_for_tool','waiting_for_approval')",
                client_id,
            )
            if pending >= self.max_pending:
                raise WorkflowConflict("Client pending-workflow capacity is full")
            row = await connection.fetchrow(
                "insert into public.workflow_jobs(id,client_id,idempotency_key,input_hash,input,"
                "deadline,max_attempts) values($1,$2,$3,$4,$5::jsonb,"
                "now()+$6*interval '1 second',$7) "
                "on conflict(client_id,idempotency_key) do nothing returning *",
                uuid4(), client_id, key, checksum, encode(payload), deadline_seconds,
                min(128, payload.get("max_steps", 4) + payload.get("max_tool_calls", 16) + 3),
            )
            if row:
                await self._event(connection, row["id"], 0, "queued", {})
                return unpack(row)
            row = await connection.fetchrow(
                "select * from public.workflow_jobs where client_id=$1 and idempotency_key=$2",
                client_id, key,
            )
            if row["input_hash"] != checksum:
                raise WorkflowConflict("Idempotency key already used with different input")
            return unpack(row)

    async def get(self, client_id, job_id):
        return unpack(await self.pool.fetchrow(
            "select * from public.workflow_jobs where id=$1 and client_id=$2", job_id, client_id,
        ))

    async def claim(self, owner, lease_seconds=30):
        if not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid lease duration")
        async with self.pool.acquire() as connection, connection.transaction():
            # A single short transaction-level lock serializes scheduling decisions across
            # workers, so the per-tenant running cap cannot race across different job rows.
            await connection.execute("select pg_advisory_xact_lock(718352091)")
            row = await connection.fetchrow(
                "select * from public.workflow_jobs where status='queued' "
                "and deadline>clock_timestamp() "
                "and attempts<max_attempts and (select count(*) from public.workflow_jobs running "
                "where running.client_id=workflow_jobs.client_id and running.status='running')<$1 "
                "order by "
                "(select count(*) from public.workflow_jobs running "
                "where running.client_id=workflow_jobs.client_id and running.status='running'), "
                "created_at for update skip locked limit 1", self.per_client_capacity,
            )
            if row is None:
                return None
            claimed = await connection.fetchrow(
                "update public.workflow_jobs set status='running',lease_owner=$2,"
                "lease_expires_at=clock_timestamp()+$3*interval '1 second',version=version+1,"
                "attempts=attempts+1,updated_at=now() where id=$1 returning *",
                row["id"], owner, lease_seconds,
            )
            await self._event(connection, row["id"], claimed["version"], "running", {})
            return unpack(claimed)

    async def renew(self, job_id, owner, version, lease_seconds=30):
        if not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid lease duration")
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("select id from public.workflow_jobs where id=$1 for update",
                                      job_id)
            updated = await connection.fetchval(
                "update public.workflow_jobs set lease_expires_at=clock_timestamp()"
                "+$4*interval '1 second' where id=$1 and lease_owner=$2 and version=$3 "
                "and status='running' and lease_expires_at>clock_timestamp() "
                "and deadline>clock_timestamp() returning id",
                job_id, owner, version, lease_seconds,
            )
        if updated is None:
            raise LeaseLost("Workflow lease lost")

    async def transition(self, job_id, owner, version, status, checkpoint, result=None):
        if status not in {"completed", "failed", "waiting_for_tool", "waiting_for_approval"}:
            raise ValueError("Invalid worker transition")
        if max(len(encode(checkpoint).encode()), len(encode(result).encode())) > 256 * 1024:
            raise ValueError("Workflow output exceeds 256 KiB")
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("select id from public.workflow_jobs where id=$1 for update",
                                      job_id)
            row = await connection.fetchrow(
                "update public.workflow_jobs set status=$4,checkpoint=$5::jsonb,result=$6::jsonb,"
                "version=version+1,lease_owner=null,lease_expires_at=null,updated_at=now() "
                "where id=$1 and lease_owner=$2 and version=$3 and status='running' "
                "and lease_expires_at>clock_timestamp() and deadline>clock_timestamp() returning *",
                job_id, owner, version, status, encode(checkpoint), encode(result),
            )
            if row is None:
                raise LeaseLost("Workflow lease lost")
            await self._event(connection, job_id, row["version"], status, {})
            return unpack(row)

    async def cancel(self, client_id, job_id):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "update public.workflow_jobs set status='cancelled',version=version+1,"
                "lease_owner=null,lease_expires_at=null,updated_at=now() where id=$1 "
                "and client_id=$2 and status not in ('completed','failed','cancelled','expired') "
                "returning *", job_id, client_id,
            )
            if row:
                await self._event(connection, job_id, row["version"], "cancelled", {})
                return unpack(row)
            return unpack(await connection.fetchrow(
                "select * from public.workflow_jobs where id=$1 and client_id=$2",
                job_id, client_id,
            ))

    async def resume(self, client_id, job_id, expected_version):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval(
                "select id from public.workflow_jobs where id=$1 and client_id=$2 for update",
                job_id, client_id,
            )
            row = await connection.fetchrow(
                "update public.workflow_jobs set status='queued',version=version+1,"
                "updated_at=now() "
                "where id=$1 and client_id=$2 and version=$3 and status='waiting_for_approval' "
                "and deadline>clock_timestamp() and attempts<max_attempts returning *",
                job_id, client_id, expected_version,
            )
            if row is None:
                raise WorkflowConflict("Workflow cannot be resumed at this version")
            await self._event(connection, job_id, row["version"], "queued", {})
            return unpack(row)

    async def tool_result(self, client_id, job_id, invocation_id, result):
        if len(encode(result).encode()) > 128 * 1024:
            raise ValueError("Tool result exceeds 128 KiB")
        checksum = digest(result)
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "select * from public.workflow_jobs where id=$1 and client_id=$2 for update",
                job_id, client_id,
            )
            if row is None:
                return None
            previous = await connection.fetchval(
                "select input_hash from public.workflow_tool_results where job_id=$1 "
                "and invocation_id=$2", job_id, invocation_id,
            )
            if previous:
                if previous != checksum:
                    raise WorkflowConflict("Conflicting tool result replay")
                return unpack(row)
            checkpoint = unpack(row)["checkpoint"]
            if (row["status"] != "waiting_for_tool"
                    or checkpoint.get("invocation_id") != invocation_id):
                raise WorkflowConflict("No matching outstanding tool invocation")
            await connection.execute(
                "insert into public.workflow_tool_results(job_id,invocation_id,input_hash,result) "
                "values($1,$2,$3,$4::jsonb)", job_id, invocation_id, checksum, encode(result),
            )
            checkpoint["tool_result"] = result
            if len(encode(checkpoint).encode()) > 256 * 1024:
                raise ValueError("Workflow checkpoint exceeds 256 KiB")
            updated = await connection.fetchrow(
                "update public.workflow_jobs set status='queued',version=version+1,"
                "checkpoint=$2::jsonb,updated_at=now() where id=$1 and deadline>clock_timestamp() "
                "and attempts<max_attempts returning *", job_id, encode(checkpoint),
            )
            if updated is None:
                raise WorkflowConflict("Workflow expired")
            await self._event(connection, job_id, updated["version"], "tool_result_received", {})
            return unpack(updated)

    async def events(self, client_id, job_id, after=0):
        return [unpack(row) for row in await self.pool.fetch(
            "select e.sequence,e.kind,e.data,e.created_at from public.workflow_events e "
            "join public.workflow_jobs j on j.id=e.job_id where j.id=$1 and j.client_id=$2 "
            "and e.sequence>$3 order by e.sequence limit 100", job_id, client_id, after,
        )]

    async def recover(self):
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                "select id,deadline<=now() as expired, attempts>=max_attempts as exhausted "
                "from public.workflow_jobs "
                "where (status='running' and lease_expires_at<=now()) or "
                "(status in ('queued','waiting_for_tool','waiting_for_approval') "
                "and deadline<=now()) or (status='queued' and attempts>=max_attempts) "
                "order by updated_at for update skip locked limit 100"
            )
            for row in rows:
                # A lost worker may have dispatched a side effect: require reconciliation,
                # never blindly retry. The checkpoint is retained for explicit resume.
                status = "expired" if row["expired"] else (
                    "failed" if row["exhausted"] else "waiting_for_approval"
                )
                updated = await connection.fetchrow(
                    "update public.workflow_jobs set status=$2,lease_owner=null,"
                    "lease_expires_at=null,version=version+1,updated_at=now() "
                    "where id=$1 returning *",
                    row["id"], status,
                )
                await self._event(connection, row["id"], updated["version"], status,
                                  {"reason": "deadline" if row["expired"] else (
                                      "attempt_limit" if row["exhausted"] else "worker_lease_lost"
                                  )})
            return len(rows)

    @staticmethod
    async def _event(connection, job_id, version, kind, data):
        await connection.execute(
            "insert into public.workflow_events(job_id,sequence,kind,data) "
            "values($1,$2,$3,$4::jsonb)",
            job_id, version, kind, encode(data),
        )
