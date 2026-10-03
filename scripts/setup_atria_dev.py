"""Add the Atria provider to the dev gateway database and publish the config.

Dev/test only. Never run against production.

Reads the Atria API key from the ATRIA_API_KEY environment variable so the
secret never lands in the repository. The synthetic seed credential is rotated
to a valid envelope at the same time: the runtime decrypts every credential in
the published snapshot, and the seed row stores a plaintext placeholder.

Usage:
    ATRIA_API_KEY=atr_... .venv/bin/python scripts/setup_atria_dev.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from gateway.admin.control_plane import _snapshot_payload
from gateway.config import get_settings
from gateway.configuration.runtime_builder import RuntimeBuilder, RuntimeSnapshot
from gateway.configuration.snapshots import (
    ConfigSnapshot,
    configuration_checksum,
    stranded_models,
)
from gateway.security.credentials import CredentialCipher

ROOT = Path(__file__).resolve().parent.parent

ATRIA_PROVIDER_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
ATRIA_CREDENTIAL_ID = uuid.UUID("60000000-0000-0000-0000-000000000002")
ATRIA_PROVIDER_MODEL_ID = uuid.UUID("60000000-0000-0000-0000-000000000003")
ATRIA_ROUTE_ID = uuid.UUID("60000000-0000-0000-0000-000000000004")
SYNTHETIC_CREDENTIAL_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")

ATRIA_MODEL_ID = "Atria-Dawn-Preview"
ATRIA_BASE_URL = "https://api.atria-asi.ai"
CAPABILITIES = ["streaming", "tool_calling", "reasoning", "structured_output"]


def _dsn() -> str:
    load_dotenv(ROOT / ".env.dbtest")
    dsn = os.environ.get("POOLER")
    if not dsn:
        sys.exit("POOLER is not set in .env.dbtest")
    return dsn


def _cipher() -> CredentialCipher:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GATEWAY_CREDENTIAL_ENCRYPTION_KEY")
    if not key:
        sys.exit("GATEWAY_CREDENTIAL_ENCRYPTION_KEY is not set in .env")
    return CredentialCipher.from_base64(key)


async def _connect(dsn: str) -> asyncpg.Connection:
    # The Supabace pooler rejects repeated prepared statements.
    return await asyncpg.connect(dsn, statement_cache_size=0)


async def _install(connection: asyncpg.Connection, api_key: str) -> None:
    cipher = _cipher()

    await connection.execute(
        """
        insert into public.providers (
          id, name, provider_type, protocol, base_url, enabled, priority,
          capabilities, timeout_seconds, settings, health
        ) values ($1, 'atria', 'openai_compatible', 'openai_chat_completions', $2,
                  true, 100, $3::text[], 600, '{"auth_scheme": "default"}'::jsonb,
                  'healthy')
        on conflict (id) do update set
          name = excluded.name,
          provider_type = excluded.provider_type,
          protocol = excluded.protocol,
          base_url = excluded.base_url,
          enabled = excluded.enabled,
          priority = excluded.priority,
          capabilities = excluded.capabilities,
          timeout_seconds = excluded.timeout_seconds,
          settings = excluded.settings,
          health = excluded.health,
          updated_at = now()
        """,
        ATRIA_PROVIDER_ID,
        ATRIA_BASE_URL,
        CAPABILITIES,
    )

    await connection.execute(
        """
        insert into public.models (id, display_name, enabled, capabilities, context_window)
        values ($1, 'Atria Dawn Preview', true, $2::text[], 200000)
        on conflict (id) do update set
          display_name = excluded.display_name,
          enabled = excluded.enabled,
          capabilities = excluded.capabilities,
          context_window = excluded.context_window,
          updated_at = now()
        """,
        ATRIA_MODEL_ID,
        CAPABILITIES,
    )

    envelope = cipher.encrypt(api_key, context=f"provider-credential:{ATRIA_CREDENTIAL_ID}")
    await connection.execute(
        """
        insert into public.provider_credentials (
          id, provider_id, name, secret_version, secret_nonce, secret_ciphertext,
          masked_hint, enabled, priority, health
        ) values ($1, $2, 'atria', $3, $4, $5, 'atr_********', true, 100, 'healthy')
        on conflict (id) do update set
          provider_id = excluded.provider_id,
          name = excluded.name,
          secret_version = excluded.secret_version,
          secret_nonce = excluded.secret_nonce,
          secret_ciphertext = excluded.secret_ciphertext,
          masked_hint = excluded.masked_hint,
          enabled = excluded.enabled,
          priority = excluded.priority,
          health = excluded.health,
          updated_at = now()
        """,
        ATRIA_CREDENTIAL_ID,
        ATRIA_PROVIDER_ID,
        envelope.version,
        envelope.nonce,
        envelope.ciphertext,
    )

    await connection.execute(
        """
        insert into public.provider_models (
          id, provider_id, model_id, upstream_model_id, protocol, capabilities,
          enabled, priority, weight, pricing, settings, serves_protocols
        ) values ($1, $2, $3, $3, 'openai_chat_completions', $4::text[],
                  true, 100, 1, '{}'::jsonb, '{}'::jsonb,
                  '{openai_chat_completions}'::text[])
        on conflict (id) do update set
          provider_id = excluded.provider_id,
          model_id = excluded.model_id,
          upstream_model_id = excluded.upstream_model_id,
          protocol = excluded.protocol,
          capabilities = excluded.capabilities,
          enabled = excluded.enabled,
          priority = excluded.priority,
          weight = excluded.weight,
          serves_protocols = excluded.serves_protocols,
          updated_at = now()
        """,
        ATRIA_PROVIDER_MODEL_ID,
        ATRIA_PROVIDER_ID,
        ATRIA_MODEL_ID,
        CAPABILITIES,
    )

    await connection.execute(
        """
        insert into public.model_routes (id, model_id, provider_model_id, priority,
                                         enabled, allow_model_fallback)
        values ($1, $2, $3, 100, true, false)
        on conflict (id) do update set
          model_id = excluded.model_id,
          provider_model_id = excluded.provider_model_id,
          priority = excluded.priority,
          enabled = excluded.enabled,
          allow_model_fallback = excluded.allow_model_fallback
        """,
        ATRIA_ROUTE_ID,
        ATRIA_MODEL_ID,
        ATRIA_PROVIDER_MODEL_ID,
    )

    await connection.execute(
        """
        insert into public.credential_model_access (credential_id, provider_model_id)
        values ($1, $2)
        on conflict do nothing
        """,
        ATRIA_CREDENTIAL_ID,
        ATRIA_PROVIDER_MODEL_ID,
    )

    # The seed row stores a plaintext placeholder, not an AESGCM envelope. The
    # runtime decrypts every credential in the snapshot, so it must be rotated
    # before the configuration can build.
    synthetic = cipher.encrypt(
        "synthetic", context=f"provider-credential:{SYNTHETIC_CREDENTIAL_ID}"
    )
    await connection.execute(
        """
        update public.provider_credentials
        set secret_version = $2, secret_nonce = $3, secret_ciphertext = $4,
            updated_at = now()
        where id = $1
        """,
        SYNTHETIC_CREDENTIAL_ID,
        synthetic.version,
        synthetic.nonce,
        synthetic.ciphertext,
    )


async def _publish(connection: asyncpg.Connection) -> dict:
    async with connection.transaction(isolation="repeatable_read"):
        await connection.execute(
            "select pg_advisory_xact_lock(hashtext('config_publication'))"
        )
        payload = await _snapshot_payload(connection)
        RuntimeSnapshot.model_validate(payload)
        stranded = stranded_models(payload)
        if stranded:
            sys.exit(f"publish_would_strand_models: {stranded}")
        checksum = configuration_checksum(payload)
        await connection.execute(
            "update public.config_versions set status='superseded' where status='published'"
        )
        row = await connection.fetchrow(
            """
            insert into public.config_versions (
              status, schema_version, payload, checksum, created_by, published_at
            ) values ('published', 1, $1::jsonb, $2, null, now())
            returning id, published_at
            """,
            json.dumps(payload, default=str),
            checksum,
        )
    return {
        "version": row["id"],
        "checksum": checksum,
        "published_at": row["published_at"].isoformat(),
    }


async def _verify(dsn: str) -> None:
    connection = await _connect(dsn)
    try:
        payload = await _snapshot_payload(connection)
        RuntimeSnapshot.model_validate(payload)
        snapshot = ConfigSnapshot.create(
            version=1,
            payload=payload,
            published_at=datetime.now(UTC),
        )
        if not snapshot.verify_checksum():
            sys.exit("published payload failed checksum verification")
        settings = get_settings()
        runtime = RuntimeBuilder(
            encryption_key=settings.credential_encryption_key,
            key_pepper=settings.key_pepper,
        ).build(payload)
        print(
            "verify ok: "
            f"models={[m.id for m in runtime.model_registry.list_enabled()]} "
            f"adapters={sorted(runtime.provider_model_adapters)} "
            f"credentials={sorted(runtime.credentials)}"
        )
    finally:
        await connection.close()


async def main() -> None:
    api_key = os.environ.get("ATRIA_API_KEY")
    if not api_key:
        sys.exit("ATRIA_API_KEY is not set")
    dsn = _dsn()

    connection = await _connect(dsn)
    try:
        async with connection.transaction():
            await _install(connection, api_key)
        print("install ok: atria provider, credential, model, route installed")
    finally:
        await connection.close()

    connection = await _connect(dsn)
    try:
        published = await _publish(connection)
    finally:
        await connection.close()
    print(f"publish ok: {published}")

    await _verify(dsn)


if __name__ == "__main__":
    asyncio.run(main())
