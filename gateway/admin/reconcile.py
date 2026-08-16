import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from gateway.admin import reconcile_guards
from gateway.admin.api import _authorize
from gateway.admin.control_plane import _masked_hint
from gateway.admin.reconcile_models import (
    ProviderReconcileInput,
    ReconcileCredential,
    ReconcileMapping,
    ReconcileModel,
    ReconcileRoute,
)
from gateway.admin.reconcile_models import enum_value as _enum_value
from gateway.protocols import ClientProtocol
from gateway.security import CredentialCipher

__all__ = [
    "ProviderReconcileInput",
    "ReconcileCredential",
    "ReconcileMapping",
    "ReconcileModel",
    "ReconcileRoute",
    "router",
]

router = APIRouter(prefix="/api/admin/v1", tags=["admin-reconcile"])


async def _connection(request: Request) -> tuple[Any, Any] | JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    connection = getattr(request.state, "control_plane_connection", None)
    if connection is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    return claims, connection


@router.put("/providers/reconcile")
async def reconcile_provider(request: Request, body: ProviderReconcileInput) -> JSONResponse:
    context = await _connection(request)
    if isinstance(context, JSONResponse):
        return context
    claims, connection = context

    encryption_key = request.app.state.settings.credential_encryption_key
    try:
        if not encryption_key:
            raise ValueError("credential encryption key is missing")
        cipher = CredentialCipher.from_base64(encryption_key)
    except ValueError:
        return JSONResponse({"error": "credential_encryption_not_configured"}, status_code=503)

    conflict = await reconcile_guards.namespace_conflict(connection, body.models)
    if conflict is not None:
        return conflict
    conflict = await reconcile_guards.alias_conflict(connection, body.models)
    if conflict is not None:
        return conflict

    existing_provider_id = await connection.fetchval(
        "select id::text from public.providers where name=$1", body.name
    )
    pool_name = f"{body.name} Pool"
    if existing_provider_id is not None:
        conflict = await reconcile_guards.shared_model_conflict(
            connection, str(existing_provider_id), body.models
        )
        if conflict is not None:
            return conflict
        conflict = await reconcile_guards.topology_conflict(
            connection, str(existing_provider_id), pool_name, body
        )
        if conflict is not None:
            return conflict

    provider = await connection.fetchrow(
        """insert into public.providers(name,provider_type,protocol,base_url,enabled,priority,
             capabilities,timeout_seconds,settings)
           values($1,null,null,$2,$3,$4,'{}',$5,coalesce($6::jsonb,'{}'::jsonb))
           on conflict(name) do update set base_url=excluded.base_url,enabled=excluded.enabled,
              priority=excluded.priority,timeout_seconds=excluded.timeout_seconds,
              settings=case when $6::jsonb is null then providers.settings
                            else excluded.settings end,updated_at=now()
           returning id""",
        body.name,
        str(body.base_url).rstrip("/"),
        body.enabled,
        body.priority,
        body.timeout_seconds,
        json.dumps(body.settings) if body.settings is not None else None,
    )
    provider_id = str(provider["id"])
    if existing_provider_id is None:
        conflict = await reconcile_guards.shared_model_conflict(
            connection, provider_id, body.models
        )
        if conflict is not None:
            return conflict
    credential_ids: list[str] = []
    credential_priorities: dict[str, int] = {}
    for item in body.credentials:
        existing = await connection.fetchrow(
            "select id from public.provider_credentials where provider_id=$1 and name=$2",
            provider_id,
            item.name,
        )
        if existing is None and item.secret is None:
            return JSONResponse(
                {"error": "new_credentials_require_secret", "credential": item.name},
                status_code=422,
            )
        if existing is None:
            credential_id = str(await connection.fetchval("select gen_random_uuid()"))
            envelope = cipher.encrypt(item.secret, context=f"provider-credential:{credential_id}")
            await connection.execute(
                """insert into public.provider_credentials(id,provider_id,name,secret_version,
                   secret_nonce,secret_ciphertext,masked_hint,enabled,priority,quota_limit,
                   quota_threshold,requests_per_minute,tokens_per_minute)
                   values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                credential_id,
                provider_id,
                item.name,
                envelope.version,
                envelope.nonce,
                envelope.ciphertext,
                _masked_hint(item.secret),
                item.enabled,
                item.priority,
                item.quota_limit,
                item.quota_threshold,
                item.requests_per_minute,
                item.tokens_per_minute,
            )
        else:
            credential_id = str(existing["id"])
            if item.secret is not None and item.rotate_secret:
                envelope = cipher.encrypt(
                    item.secret, context=f"provider-credential:{credential_id}"
                )
                await connection.execute(
                    """update public.provider_credentials set secret_version=$2,secret_nonce=$3,
                       secret_ciphertext=$4,masked_hint=$5,updated_at=now() where id=$1""",
                    credential_id,
                    envelope.version,
                    envelope.nonce,
                    envelope.ciphertext,
                    _masked_hint(item.secret),
                )
            await connection.execute(
                """update public.provider_credentials set enabled=$2,priority=$3,quota_limit=$4,
                   quota_threshold=$5,requests_per_minute=$6,tokens_per_minute=$7,updated_at=now()
                   where id=$1""",
                credential_id,
                item.enabled,
                item.priority,
                item.quota_limit,
                item.quota_threshold,
                item.requests_per_minute,
                item.tokens_per_minute,
            )
        credential_ids.append(credential_id)
        credential_priorities[credential_id] = item.priority

    await connection.execute(
        """update public.provider_credentials
           set enabled=false,updated_at=now()
           where provider_id=$1 and not (name = any($2::text[]))""",
        provider_id,
        [item.name for item in body.credentials],
    )

    model_ids: set[str] = set()
    for item in body.models:
        model_ids.add(item.id)
        requested_capabilities = {
            _enum_value(capability) for capability in item.capabilities
        }
        await connection.execute(
            """insert into public.models(id,display_name,enabled,capabilities,context_window)
                values($1,coalesce($2,$1),$3,$4,$5)
                on conflict(id) do update set
                  display_name=coalesce($2,models.display_name),
                  enabled=case when $7 then excluded.enabled else models.enabled end,
                  capabilities=excluded.capabilities,
                  context_window=coalesce($5,models.context_window),updated_at=now()
                where not exists (
                  select 1 from public.provider_models existing_pm
                  where existing_pm.model_id=models.id and existing_pm.provider_id <> $6
                )""",
            item.id,
            item.display_name or item.id,
            item.enabled if item.enabled is not None else True,
            sorted(requested_capabilities),
            item.context_window,
            provider_id,
            "enabled" in item.model_fields_set,
        )
        for alias in item.aliases:
            await connection.execute(
                """insert into public.model_aliases(alias,model_id) values($1,$2)
                   on conflict(alias) do nothing""",
                alias,
                item.id,
            )

    mapping_ids: dict[tuple[str, str, ClientProtocol], str] = {}
    mapping_weights: dict[str, float] = {}
    for item in body.mappings:
        mapping = await connection.fetchrow(
            """insert into public.provider_models(provider_id,model_id,upstream_model_id,protocol,
                 capabilities,enabled,priority,weight,max_concurrency,settings,pricing)
               values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb)
               on conflict(provider_id,model_id,upstream_model_id,protocol) do update set
                 capabilities=excluded.capabilities,enabled=excluded.enabled,priority=excluded.priority,
                 weight=excluded.weight,max_concurrency=excluded.max_concurrency,settings=excluded.settings,
                 pricing=excluded.pricing,updated_at=now()
               returning id""",
            provider_id,
            item.model_id,
            item.upstream_model_id,
            item.protocol.value,
            [_enum_value(capability) for capability in item.capabilities],
            item.enabled,
            item.priority,
            item.weight,
            item.max_concurrency,
            json.dumps(item.settings),
            json.dumps(item.pricing),
        )
        mapping_ids[(item.model_id, item.upstream_model_id, item.protocol)] = str(mapping["id"])
        mapping_weights[str(mapping["id"])] = item.weight
        for credential_id in credential_ids:
            await connection.execute(
                """insert into public.credential_model_access(credential_id,provider_model_id)
                   values($1,$2) on conflict do nothing""",
                credential_id,
            mapping["id"],
            )

    desired_mapping_models = [item.model_id for item in body.mappings]
    desired_mapping_upstreams = [item.upstream_model_id for item in body.mappings]
    desired_mapping_protocols = [item.protocol.value for item in body.mappings]
    await connection.execute(
        """update public.provider_models pm
           set enabled=false,updated_at=now()
           where pm.provider_id=$1
             and not exists (
               select 1 from unnest($2::text[],$3::text[],$4::text[])
                 as desired(model_id,upstream_model_id,protocol)
               where desired.model_id=pm.model_id
                 and desired.upstream_model_id=pm.upstream_model_id
                 and desired.protocol=pm.protocol
             )""",
        provider_id,
        desired_mapping_models,
        desired_mapping_upstreams,
        desired_mapping_protocols,
    )
    desired_access_mapping_ids = [
        mapping_id for mapping_id in mapping_ids.values() for _ in credential_ids
    ]
    desired_access_credential_ids = [
        credential_id for _ in mapping_ids.values() for credential_id in credential_ids
    ]
    await connection.execute(
        """delete from public.credential_model_access cma
           using public.provider_credentials c,public.provider_models pm
           where c.id=cma.credential_id and pm.id=cma.provider_model_id
             and c.provider_id=$1 and pm.provider_id=$1
             and not exists (
               select 1 from unnest($2::uuid[],$3::uuid[])
                 as desired(provider_model_id,credential_id)
               where desired.provider_model_id=cma.provider_model_id
                 and desired.credential_id=cma.credential_id
             )""",
        provider_id,
        desired_access_mapping_ids,
        desired_access_credential_ids,
    )

    conflicting_pool_provider = await connection.fetchval(
        """select pm.provider_id::text
           from public.provider_pools pp
           join public.provider_pool_members ppm on ppm.pool_id=pp.id
           join public.provider_models pm on pm.id=ppm.provider_model_id
           where pp.name=$1 and pm.provider_id <> $2
           limit 1""",
        pool_name,
        provider_id,
    )
    if conflicting_pool_provider is not None:
        return JSONResponse(
            {"error": "provider_pool_ownership_conflict", "pool_name": pool_name},
            status_code=409,
        )
    pool = await connection.fetchrow(
        """insert into public.provider_pools(name,enabled,strategy,settings)
           values($1,$2,$3,$4::jsonb)
           on conflict(name) do update set enabled=excluded.enabled,strategy=excluded.strategy,
             settings=excluded.settings,updated_at=now() returning id""",
        pool_name,
        body.pool_enabled,
        body.pool_strategy,
        json.dumps({"health_aware": body.health_aware, "quota_aware": body.quota_aware}),
    )
    for mapping_id in mapping_ids.values():
        for credential_id in credential_ids:
            await connection.execute(
                """insert into public.provider_pool_members(
                    pool_id,provider_model_id,credential_id,priority,weight)
                    values($1,$2,$3,$4,$5)
                    on conflict(pool_id,provider_model_id,credential_id) do update
                    set enabled=true,draining=false,priority=excluded.priority,
                        weight=excluded.weight,updated_at=now()""",
                pool["id"],
                mapping_id,
                credential_id,
                credential_priorities[credential_id],
                mapping_weights[mapping_id],
            )
    desired_pool_mapping_ids = list(mapping_ids.values())
    await connection.execute(
        """update public.provider_pool_members ppm
           set enabled=false,updated_at=now()
           where ppm.pool_id=$1
             and not exists (
               select 1 from unnest($2::uuid[],$3::uuid[])
                 as desired(provider_model_id,credential_id)
               where desired.provider_model_id=ppm.provider_model_id
                 and desired.credential_id=ppm.credential_id
             )""",
        pool["id"],
        [mapping_id for mapping_id in desired_pool_mapping_ids for _ in credential_ids],
        [credential_id for _ in desired_pool_mapping_ids for credential_id in credential_ids],
    )
    for item in body.routes:
        mapping_id = mapping_ids[
            (item.model_id, item.mapping_upstream_model_id, item.mapping_protocol)
        ]
        await connection.execute(
            """insert into public.model_routes(model_id,provider_model_id,priority,enabled,
                 allow_model_fallback,pool_id)
               values($1,$2,$3,$4,$5,$6)
               on conflict(model_id,provider_model_id) do update set priority=excluded.priority,
                 enabled=excluded.enabled,allow_model_fallback=excluded.allow_model_fallback,
                 pool_id=excluded.pool_id""",
            item.model_id,
            mapping_id,
            item.priority,
            item.enabled,
            item.allow_model_fallback,
            pool["id"],
        )
    desired_route_model_ids = [item.model_id for item in body.routes]
    desired_route_mapping_ids = [
        mapping_ids[
            (item.model_id, item.mapping_upstream_model_id, item.mapping_protocol)
        ]
        for item in body.routes
    ]
    await connection.execute(
        """update public.model_routes r
           set enabled=false
           where exists (
               select 1 from public.provider_models pm
               where pm.id=r.provider_model_id and pm.provider_id=$1
             )
             and not exists (
               select 1 from unnest($2::text[],$3::uuid[])
                 as desired(model_id,provider_model_id)
               where desired.model_id=r.model_id
                 and desired.provider_model_id=r.provider_model_id
             )""",
        provider_id,
        desired_route_model_ids,
        desired_route_mapping_ids,
    )
    await connection.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id,metadata)
           values($1,'provider_reconciled','provider',$2,$3::jsonb)""",
        claims.subject,
        provider_id,
        json.dumps(
            {
                "credential_count": len(credential_ids),
                "model_count": len(model_ids),
                "mapping_count": len(mapping_ids),
            }
        ),
    )
    return JSONResponse(
        jsonable_encoder(
            {
                "provider_id": provider_id,
                "pool_id": str(pool["id"]),
                "credential_count": len(credential_ids),
                "model_count": len(model_ids),
                "mapping_count": len(mapping_ids),
                "route_count": len(body.routes),
            }
        )
    )
