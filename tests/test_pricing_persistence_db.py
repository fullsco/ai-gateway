"""Real-PostgreSQL regression tests for provider-model pricing persistence.

These guard the defect that emptied AgentRouter pricing and then wrote json-null:
a save that omits pricing must PRESERVE the stored rate, and "omitted" must never be
encoded as the JSON value null (``'null'::jsonb``) - a value ``coalesce`` keeps, which
then fails ``SnapshotProviderModel`` validation as ``pricing=None`` and surfaces as
"provider_models.N.pricing: Input should be a valid dictionary" on publish.

The fake-pool tests pin the SQL *text* the handlers emit; these run the exact
``coalesce`` constructs against a live Postgres so the *semantics* are proven.
Everything runs inside a transaction that is rolled back - nothing persists.

Mirrors the pricing SQL in:
  - gateway/admin/control_plane.py  update_provider_model  pricing=coalesce($13::jsonb, pricing)
  - gateway/admin/reconcile.py      mapping upsert          pricing=coalesce($12::jsonb, ...)
"""
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from gateway.configuration import create_pool
from gateway.configuration.runtime_builder import SnapshotProviderModel
from gateway.protocols import Capability, ClientProtocol

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PRICED = {"input_per_million": 3, "output_per_million": 15, "currency": "USD"}


@asynccontextmanager
async def provider_model(pricing):
    """Seed one synthetic provider_model carrying ``pricing``; yield (conn, id); roll back."""
    dsn = os.environ.get("GATEWAY_DATABASE_URL")
    if not dsn:
        pytest.skip("Development database required")
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute("set local lock_timeout='3s'")
                await conn.execute("set local statement_timeout='10s'")
                await conn.execute("set local idle_in_transaction_session_timeout='30s'")
                provider = await conn.fetchval(
                    "insert into public.providers(name,provider_type,protocol,base_url) values "
                    "($1,'openai_compatible','openai_chat_completions','http://127.0.0.1') "
                    "returning id",
                    f"synthetic-{uuid4()}",
                )
                # provider_models.model_id is FK -> models.id; seed the catalogue row first.
                model_slug = f"synthetic-{uuid4()}"
                await conn.execute(
                    "insert into public.models(id,display_name) values($1,$1)",
                    model_slug,
                )
                pm_id = await conn.fetchval(
                    "insert into public.provider_models"
                    "(provider_id,model_id,upstream_model_id,protocol,capabilities,pricing) values "
                    "($1,$2,$2,'anthropic_messages','{streaming}',$3::jsonb) returning id",
                    provider,
                    model_slug,
                    json.dumps(pricing),
                )
                yield conn, pm_id
            finally:
                await tx.rollback()
    finally:
        await pool.close()


async def _pricing(conn, pm_id):
    """Read pricing back as (python_value, jsonb_typeof)."""
    row = await conn.fetchrow(
        "select pricing, jsonb_typeof(pricing) as t from public.provider_models where id=$1",
        pm_id,
    )
    value = row["pricing"]
    if isinstance(value, str):
        value = json.loads(value)
    return value, row["t"]


# --- control_plane update path: pricing=coalesce($N::jsonb, pricing) -----------------

async def test_omitted_pricing_preserves_rate_and_never_becomes_json_null():
    async with provider_model(PRICED) as (conn, pm_id):
        # Omitted price => the handler binds SQL NULL (None), never the string "null".
        await conn.execute(
            "update public.provider_models set pricing=coalesce($2::jsonb, pricing), "
            "updated_at=now() where id=$1",
            pm_id,
            None,
        )
        value, typeof = await _pricing(conn, pm_id)
        assert value == PRICED       # stored rate preserved
        assert typeof == "object"    # never degraded to json-null


async def test_explicit_pricing_replaces_the_rate():
    async with provider_model(PRICED) as (conn, pm_id):
        new = {"per_request": "0.20", "currency": "USD"}
        await conn.execute(
            "update public.provider_models set pricing=coalesce($2::jsonb, pricing), "
            "updated_at=now() where id=$1",
            pm_id,
            json.dumps(new),
        )
        value, typeof = await _pricing(conn, pm_id)
        assert value == new
        assert typeof == "object"


async def test_the_json_null_string_is_what_corrupts_the_row():
    """Witness for the fix: the old ``else "null"`` degraded pricing to json-null."""
    async with provider_model(PRICED) as (conn, pm_id):
        await conn.execute(
            "update public.provider_models set pricing=coalesce($2::jsonb, pricing) where id=$1",
            pm_id,
            "null",  # the defect: JSON string "null" -> 'null'::jsonb, which coalesce keeps
        )
        value, typeof = await _pricing(conn, pm_id)
        assert typeof == "null"      # coalesce KEPT json-null; the rate is gone
        assert value is None         # reads back as Python None -> breaks the snapshot


# --- reconcile upsert path: values(...coalesce($12,'{}')) on conflict do update -----

RECONCILE_UPSERT = (
    "insert into public.provider_models"
    "(provider_id,model_id,upstream_model_id,protocol,capabilities,pricing) values "
    "($1,$2,$3,'anthropic_messages','{streaming}',coalesce($4::jsonb,'{}'::jsonb)) "
    "on conflict(provider_id,model_id,upstream_model_id,protocol) do update set "
    "pricing=coalesce($4::jsonb, public.provider_models.pricing),updated_at=now() returning id"
)

# The pre-fix conflict clause, kept to prove why reading excluded.pricing was wrong.
RECONCILE_UPSERT_BUGGY = RECONCILE_UPSERT.replace(
    "pricing=coalesce($4::jsonb, public.provider_models.pricing)",
    "pricing=coalesce(excluded.pricing, public.provider_models.pricing)",
)


async def _identity(conn, pm_id):
    return await conn.fetchrow(
        "select provider_id,model_id,upstream_model_id from public.provider_models where id=$1",
        pm_id,
    )


async def test_reconcile_upsert_omitted_preserves_existing_rate():
    async with provider_model(PRICED) as (conn, pm_id):
        ident = await _identity(conn, pm_id)
        # Re-save the SAME mapping with pricing omitted (param None).
        await conn.execute(
            RECONCILE_UPSERT,
            ident["provider_id"],
            ident["model_id"],
            ident["upstream_model_id"],
            None,
        )
        value, typeof = await _pricing(conn, pm_id)
        assert value == PRICED
        assert typeof == "object"


async def test_reconcile_upsert_explicit_replaces_rate():
    async with provider_model(PRICED) as (conn, pm_id):
        ident = await _identity(conn, pm_id)
        new = {"blended_per_million": "1.50", "currency": "USD"}
        await conn.execute(
            RECONCILE_UPSERT,
            ident["provider_id"],
            ident["model_id"],
            ident["upstream_model_id"],
            json.dumps(new),
        )
        value, _ = await _pricing(conn, pm_id)
        assert value == new


async def test_reconcile_excluded_pricing_clause_would_have_wiped_the_rate():
    """excluded.pricing is coalesced to {} in VALUES, so the old conflict clause
    overwrote the stored rate with {} even when the raw param was NULL."""
    async with provider_model(PRICED) as (conn, pm_id):
        ident = await _identity(conn, pm_id)
        await conn.execute(
            RECONCILE_UPSERT_BUGGY,
            ident["provider_id"],
            ident["model_id"],
            ident["upstream_model_id"],
            None,
        )
        value, typeof = await _pricing(conn, pm_id)
        assert value == {}           # wiped, not preserved
        assert typeof == "object"


# --- the payoff: a preserved row builds, and a json-null row reads as unpriced ---

def _snapshot(pricing):
    return SnapshotProviderModel(
        id=str(uuid4()),
        canonical_model_id="m",
        provider_id=str(uuid4()),
        upstream_model_id="u",
        protocol=ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities=frozenset({Capability.STREAMING}),
        pricing=pricing,
    )


async def test_saved_row_builds_snapshot_and_json_null_reads_as_unpriced():
    async with provider_model(PRICED) as (conn, pm_id):
        await conn.execute(
            "update public.provider_models set pricing=coalesce($2::jsonb, pricing) where id=$1",
            pm_id,
            None,
        )
        value, _ = await _pricing(conn, pm_id)
        assert _snapshot(value).pricing == PRICED      # preserved dict builds cleanly

    # A row holding the jsonb *value* null decodes to None, which is what the
    # publish validator used to reject with "provider_models.N.pricing: Input
    # should be a valid dictionary", 422-ing the whole configuration because one
    # unpriced mapping existed. Unpriced is a legitimate state, so it must build.
    assert _snapshot(None).pricing == {}
    # ... and so must the raw column text, which is what the pool actually hands
    # back: there is no jsonb codec, so both NULL and 'null'::jsonb arrive as the
    # string 'null' and only reach Python None via json.loads.
    assert _snapshot("null").pricing == {}
    # A real rate is untouched by the normalization.
    assert _snapshot(PRICED).pricing == PRICED
    assert _snapshot(json.dumps(PRICED)).pricing == PRICED


