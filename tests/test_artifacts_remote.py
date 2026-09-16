import asyncio
from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidTag

from gateway.artifacts import ArtifactStore
from gateway.workflow_store import WorkflowStore
from scripts.seed_dbtest import CLIENT
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_artifacts_are_encrypted_tenant_bound_integrity_checked_and_expirable():
    async with dedicated_pool() as pool:
        jobs = WorkflowStore(pool)
        job = await jobs.submit(CLIENT, str(uuid4()), {"synthetic": True})
        store = ArtifactStore(pool, b"a" * 32)
        try:
            artifact = await store.put(CLIENT, job["id"], b"synthetic secret content")
            artifact_id = UUID(artifact["id"])
            assert await store.get(CLIENT, job["id"], artifact_id) == b"synthetic secret content"
            assert await store.get(uuid4(), job["id"], artifact_id) is None
            assert await store.get(CLIENT, uuid4(), artifact_id) is None
            ciphertext = await pool.fetchval(
                "select ciphertext from public.workflow_artifacts where id=$1", artifact_id,
            )
            assert b"synthetic secret content" not in ciphertext
            with pytest.raises(InvalidTag):
                await ArtifactStore(pool, b"b" * 32).get(CLIENT, job["id"], artifact_id)
            with pytest.raises(ValueError, match="exceeds"):
                await store.put(CLIENT, job["id"], b"x" * (ArtifactStore.MAX_BYTES + 1))
            await pool.execute(
                "update public.workflow_artifacts set expires_at=now()-interval '1 second' "
                "where id=$1", artifact_id,
            )
            assert await store.get(CLIENT, job["id"], artifact_id) is None
            assert await store.collect_expired() == "DELETE 1"
        finally:
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


async def test_empty_artifacts_are_count_bounded_under_concurrent_uploads():
    async with dedicated_pool() as pool:
        job = await WorkflowStore(pool).submit(CLIENT, str(uuid4()), {"synthetic": True})
        store = ArtifactStore(pool, b"a" * 32)
        store.MAX_JOB_OBJECTS = 1
        try:
            results = await asyncio.gather(
                store.put(CLIENT, job["id"], b""), store.put(CLIENT, job["id"], b""),
                return_exceptions=True,
            )
            assert sum(isinstance(result, ValueError) for result in results) == 1
            assert await pool.fetchval(
                "select count(*) from public.workflow_artifacts where job_id=$1", job["id"],
            ) == 1
        finally:
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])
