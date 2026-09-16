"""Deterministic synthetic metadata, no provider secrets or external endpoints."""

import hashlib
import json
from uuid import UUID

CLIENT = UUID("10000000-0000-0000-0000-000000000001")
PROVIDER = UUID("20000000-0000-0000-0000-000000000001")
CREDENTIAL = UUID("30000000-0000-0000-0000-000000000001")
MAPPING = UUID("40000000-0000-0000-0000-000000000001")
ACTOR = UUID("50000000-0000-0000-0000-000000000001")


async def seed(db):
    async with db.transaction():
        await db.execute("set local lock_timeout='3s'")
        await db.execute("insert into auth.users(id) values($1) on conflict(id) do nothing", ACTOR)
        await db.execute(
            "insert into public.gateway_clients(id,name,allowed_protocols) "
            "values($1,'Synthetic Qualification',array['openai_chat_completions']) "
            "on conflict(id) do nothing", CLIENT,
        )
        await db.execute(
            "insert into public.providers(id,name,base_url,provider_type,protocol) "
            "values($1,'SyntheticProvider','http://127.0.0.1','openai_compatible',"
            "'openai_chat_completions') on conflict(id) do nothing", PROVIDER,
        )
        await db.execute(
            "insert into public.provider_credentials(id,provider_id,name,secret_version,"
            "secret_nonce,secret_ciphertext) values($1,$2,'synthetic',1,'synthetic','synthetic') "
            "on conflict(id) do nothing", CREDENTIAL, PROVIDER,
        )
        await db.execute(
            "insert into public.models(id,display_name) values('qualification-model','Synthetic') "
            "on conflict(id) do nothing"
        )
        await db.execute(
            "insert into public.provider_models"
            "(id,provider_id,model_id,upstream_model_id,protocol) "
            "values($1,$2,'qualification-model','synthetic','openai_chat_completions') "
            "on conflict(id) do nothing", MAPPING, PROVIDER,
        )
        await db.execute(
            "insert into public.model_routes(model_id,provider_model_id,allow_model_fallback) "
            "values('qualification-model',$1,true) on conflict do nothing", MAPPING,
        )
        payload = {"clients": [{"id": str(CLIENT), "name": "Synthetic Qualification"}],
                   "provider_models": [{"id": str(MAPPING)}]}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if not await db.fetchval("select exists(select 1 from public.config_versions)"):
            await db.execute(
                "insert into public.config_versions(status,schema_version,payload,checksum,"
                "published_at) values('published',1,$1::jsonb,$2,now())",
                body, hashlib.sha256(body.encode()).hexdigest(),
            )
