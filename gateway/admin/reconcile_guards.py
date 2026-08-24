from typing import Any

from fastapi.responses import JSONResponse

from gateway.admin.reconcile_models import (
    ProviderReconcileInput,
    ReconcileModel,
    enum_value,
    normalized_name,
)


async def alias_conflict(connection: Any, models: list[ReconcileModel]) -> JSONResponse | None:
    requested = {
        normalized_name(alias): model.id for model in models for alias in model.aliases
    }
    if not requested:
        return None
    normalized_aliases = sorted(requested)
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(alias,0)) from unnest($1::text[]) alias",
        normalized_aliases,
    )
    rows = await connection.fetch(
        "select alias,model_id from public.model_aliases where lower(alias)=any($1::text[])",
        normalized_aliases,
    )
    for row in rows:
        requested_model_id = requested[normalized_name(row["alias"])]
        if row["model_id"] != requested_model_id:
            return JSONResponse(
                {
                    "error": "model_alias_ownership_conflict",
                    "alias": row["alias"],
                    "existing_model_id": row["model_id"],
                    "requested_model_id": requested_model_id,
                },
                status_code=409,
            )
    return None


async def namespace_conflict(
    connection: Any, models: list[ReconcileModel]
) -> JSONResponse | None:
    requested_models = {normalized_name(model.id): model.id for model in models}
    requested_aliases = {
        normalized_name(alias): model.id for model in models for alias in model.aliases
    }
    names = sorted(requested_models.keys() | requested_aliases.keys())
    if not names:
        return None
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(name,0)) from unnest($1::text[]) name",
        names,
    )
    existing_models = await connection.fetch(
        "select id from public.models where lower(id)=any($1::text[])", names
    )
    for row in existing_models:
        normalized = normalized_name(row["id"])
        owner = requested_aliases.get(normalized)
        requested_id = requested_models.get(normalized)
        if owner is not None and owner != row["id"]:
            return _namespace_response(row["id"], row["id"], owner)
        if requested_id is not None and requested_id != row["id"]:
            return _namespace_response(requested_id, row["id"], requested_id)
    existing_aliases = await connection.fetch(
        "select alias,model_id from public.model_aliases where lower(alias)=any($1::text[])",
        names,
    )
    for row in existing_aliases:
        requested_id = requested_models.get(normalized_name(row["alias"]))
        if requested_id is not None and requested_id != row["model_id"]:
            return _namespace_response(requested_id, row["model_id"], requested_id)
    return None


def _namespace_response(name: str, existing: str, requested: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": "model_namespace_conflict",
            "name": name,
            "existing_model_id": existing,
            "requested_model_id": requested,
        },
        status_code=409,
    )


async def shared_model_conflict(
    connection: Any, provider_id: str, models: list[ReconcileModel]
) -> JSONResponse | None:
    if not models:
        return None
    rows = await connection.fetch(
        """select m.id,m.display_name,m.enabled,m.capabilities,m.context_window,
                  array(select a.alias from public.model_aliases a
                        where a.model_id=m.id order by a.alias) as aliases
           from public.models m where m.id=any($1::text[])
             and exists (select 1 from public.provider_models pm
                         where pm.model_id=m.id and pm.provider_id <> $2)""",
        [model.id for model in models],
        provider_id,
    )
    requested = {model.id: model for model in models}
    for row in rows:
        model = requested[row["id"]]
        if _model_differs(model, row):
            return JSONResponse(
                {"error": "shared_model_metadata_conflict", "model_id": model.id},
                status_code=409,
            )
    return None


def _model_differs(model: ReconcileModel, row: Any) -> bool:
    return (
        ("display_name" in model.model_fields_set and model.display_name != row["display_name"])
        or ("enabled" in model.model_fields_set and model.enabled != row["enabled"])
        or (
            "capabilities" in model.model_fields_set
            and {enum_value(value) for value in model.capabilities}
            != set(row["capabilities"] or [])
        )
        or (
            "context_window" in model.model_fields_set
            and model.context_window != row["context_window"]
        )
        or (
            "aliases" in model.model_fields_set
            and {normalized_name(value) for value in model.aliases}
            != {normalized_name(value) for value in row["aliases"] or []}
        )
    )


async def topology_conflict(
    connection: Any, provider_id: str, pool_name: str, body: ProviderReconcileInput
) -> JSONResponse | None:
    reason = await connection.fetchval(
        _TOPOLOGY_QUERY,
        provider_id,
        pool_name,
        [credential.name for credential in body.credentials],
        [mapping.model_id for mapping in body.mappings],
        [mapping.upstream_model_id for mapping in body.mappings],
        [mapping.protocol.value for mapping in body.mappings],
        [route.model_id for route in body.routes],
        [route.mapping_upstream_model_id for route in body.routes],
        [route.mapping_protocol.value for route in body.routes],
    )
    if reason is None:
        return None
    return JSONResponse(
        {"error": "provider_topology_not_supported", "reason": reason}, status_code=409
    )


_TOPOLOGY_QUERY = """with provider_credentials as (
  select id from public.provider_credentials where provider_id=$1 and name=any($3::text[])
), provider_mappings as (
  select pm.id from public.provider_models pm
  join unnest($4::text[],$5::text[],$6::text[]) desired(model_id,upstream_model_id,protocol)
    on desired.model_id=pm.model_id and desired.upstream_model_id=pm.upstream_model_id
   and desired.protocol=pm.protocol where pm.provider_id=$1
), managed_pool as (select id from public.provider_pools where name=$2),
desired_routes as (
  select pm.id from public.provider_models pm
  join unnest($7::text[],$8::text[],$9::text[]) desired(model_id,upstream_model_id,protocol)
    on desired.model_id=pm.model_id and desired.upstream_model_id=pm.upstream_model_id
   and desired.protocol=pm.protocol where pm.provider_id=$1
)
select case
 when exists (select 1 from public.model_routes r join desired_routes dr
              on dr.id=r.provider_model_id where r.pool_id is distinct from
              (select id from managed_pool)) then 'custom_route_pool'
 when exists (select 1 from public.provider_pools pp join managed_pool mp
              on mp.id=pp.id where pp.model_id is not null
              or pp.settings - 'health_aware' - 'quota_aware' <> '{}'::jsonb)
   then 'custom_pool_configuration'
 when exists (select 1 from public.provider_pool_members ppm join managed_pool mp
              on mp.id=ppm.pool_id where not ppm.enabled or ppm.draining)
   then 'member_operational_state'
 -- Selective access means somebody has deliberately restricted which credentials
 -- may serve which mappings, and reconciling would flatten that. The absence of any
 -- access row is the opposite: no restriction at all, which is how every provider
 -- created before this flow existed looks, and what the router itself treats as
 -- unrestricted. Reading absence as selective made those providers permanently
 -- unmanageable: adding a model or a credential to hcnsec, GoRouter, TabiAi or
 -- AgentRouter returned provider_topology_not_supported forever, because a complete
 -- matrix is only ever written when a provider is first created.
 when exists (select 1 from public.credential_model_access cma
              join provider_credentials c on c.id=cma.credential_id)
  and exists (select 1 from provider_credentials c cross join provider_mappings pm where not exists
              (select 1 from public.credential_model_access cma where cma.credential_id=c.id
               and cma.provider_model_id=pm.id)) then 'selective_credential_access'
 -- Same reasoning for pool membership: no members means no curated pool to preserve.
 when exists (select 1 from public.provider_pool_members ppm join managed_pool mp
              on mp.id=ppm.pool_id)
  and exists (select 1 from provider_credentials c cross join provider_mappings pm where not exists
              (select 1 from public.provider_pool_members ppm join managed_pool mp
               on mp.id=ppm.pool_id where ppm.credential_id=c.id
               and ppm.provider_model_id=pm.id)) then 'selective_pool_membership'
end"""
