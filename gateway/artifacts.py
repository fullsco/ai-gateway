"""Encrypted, tenant-bound artifact objects stored separately from workflow checkpoints."""

import hashlib
import os
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ArtifactStore:
    MAX_BYTES = 1024 * 1024
    MAX_JOB_BYTES = 8 * MAX_BYTES
    MAX_JOB_OBJECTS = 64

    def __init__(self, pool, key: bytes):
        if len(key) != 32:
            raise ValueError("Artifact encryption requires a 32-byte key")
        self.pool = pool
        self.cipher = AESGCM(key)

    @staticmethod
    def aad(client, job, artifact):
        return f"artifact:v1:{UUID(str(client))}:{UUID(str(job))}:{UUID(str(artifact))}".encode()

    async def put(self, client, job, content: bytes, ttl_seconds=86400):
        if len(content) > self.MAX_BYTES:
            raise ValueError("Artifact exceeds 1 MiB")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 604800:
            raise ValueError("Artifact retention must be 1–604800 seconds")
        artifact, nonce = uuid4(), os.urandom(12)
        digest = hashlib.sha256(content).hexdigest()
        ciphertext = self.cipher.encrypt(nonce, content, self.aad(client, job, artifact))
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval(
                "select id from public.gateway_clients where id=$1 for update", client,
            )
            owned = await connection.fetchval(
                "select id from public.workflow_jobs where id=$1 and client_id=$2 for update",
                job, client,
            )
            if owned is None:
                return None
            usage = await connection.fetchrow(
                "select coalesce(sum(greatest(size_bytes,1024)),0) as bytes,count(*) as objects "
                "from public.workflow_artifacts where job_id=$1", job,
            )
            if (usage["bytes"] + max(len(content), 1024) > self.MAX_JOB_BYTES
                    or usage["objects"] >= self.MAX_JOB_OBJECTS):
                raise ValueError("Workflow artifact storage limit exceeded")
            await connection.execute(
                "insert into public.workflow_artifacts(id,client_id,job_id,sha256,size_bytes,"
                "nonce,ciphertext,expires_at) values($1,$2,$3,$4,$5,$6,$7,"
                "now()+$8*interval '1 second')",
                artifact, client, job, digest, len(content), nonce, ciphertext, ttl_seconds,
            )
        return {"id": str(artifact), "sha256": digest, "size_bytes": len(content)}

    async def get(self, client, job, artifact):
        row = await self.pool.fetchrow(
            "select a.* from public.workflow_artifacts a join public.workflow_jobs j "
            "on j.id=a.job_id where a.id=$1 and a.job_id=$2 and a.client_id=$3 "
            "and j.client_id=$3 and a.expires_at>now()", artifact, job, client,
        )
        if row is None:
            return None
        content = self.cipher.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]),
                                      self.aad(client, job, artifact))
        if (len(content) != row["size_bytes"]
                or hashlib.sha256(content).hexdigest() != row["sha256"]):
            raise ValueError("Artifact integrity check failed")
        return content

    async def collect_expired(self):
        return await self.pool.execute(
            "delete from public.workflow_artifacts where id in "
            "(select id from public.workflow_artifacts where expires_at<=now() "
            "order by expires_at limit 100 for update skip locked)"
        )
