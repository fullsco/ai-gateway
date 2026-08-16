import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from gateway.admin.api import _authorize
from gateway.admin.auth import AdminClaims
from gateway.configuration import (
    configuration_checksum,
    configuration_projection,
    legacy_checksum,
    legacy_configuration_checksum,
)
from gateway.configuration.runtime_builder import RuntimeSnapshot
from gateway.protocols import ClientProtocol
from gateway.security import CredentialCipher, GatewayKeyHasher

router = APIRouter(prefix="/api/admin/v1", tags=["admin-control"])


class NormalizedStringLists(BaseModel):
    @field_validator(
        "capabilities",
        "allowed_protocols",
        "allowed_models",
        "aliases",
        check_fields=False,
    )
    @classmethod
    def normalize_string_list(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("list values must not be blank")
        return normalized


class ProviderInput(NormalizedStringLists):
    name: str = Field(min_length=1, max_length=120)
    provider_type: Literal["anthropic_compatible", "openai_compatible"] | None = None
    protocol: ClientProtocol | None = None
    base_url: str = Field(min_length=8, max_length=500)
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    capabilities: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=600, gt=0)
    settings: dict[str, Any] = Field(default_factory=dict)


class CredentialInput(BaseModel):
    provider_id: str
    name: str = Field(min_length=1, max_length=120)
    secret: str = Field(min_length=1, repr=False)
    priority: int = Field(default=100, ge=0)
    quota_limit: float | None = Field(default=None, ge=0)
    quota_threshold: float = Field(default=0.95, gt=0, le=1)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class CredentialRotationInput(BaseModel):
    secret: str = Field(min_length=1, repr=False)


class CredentialUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    quota_limit: float | None = Field(default=None, ge=0)
    quota_threshold: float = Field(default=0.95, gt=0, le=1)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class ClientInput(NormalizedStringLists):
    name: str = Field(min_length=1, max_length=120)
    allowed_protocols: list[ClientProtocol] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=list)
    enabled: bool = True
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)
    spending_limit: float | None = Field(default=None, ge=0)


class ClientKeyInput(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_future_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value <= datetime.now(value.tzinfo):
            raise ValueError("expires_at must be in the future")
        return value


class KeyRevocationInput(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ModelInput(NormalizedStringLists):
    id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    capabilities: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    context_window: int | None = Field(default=None, gt=0)


class ProviderModelInput(NormalizedStringLists):
    provider_id: str
    model_id: str
    upstream_model_id: str
    protocol: ClientProtocol
    capabilities: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0)
    weight: float = Field(default=1, gt=0)
    enabled: bool = True
    max_concurrency: int = Field(default=8, gt=0)
    settings: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)

    @field_validator("pricing")
    @classmethod
    def validate_pricing(cls, pricing: dict[str, Any]) -> dict[str, Any]:
        if not pricing:
            return pricing
        required = {"input_per_million", "output_per_million", "currency"}
        if missing := required - pricing.keys():
            raise ValueError(f"pricing is missing: {', '.join(sorted(missing))}")
        allowed = required | {"cached_input_per_million", "version", "effective_at"}
        if extra := pricing.keys() - allowed:
            raise ValueError(f"pricing contains unsupported fields: {', '.join(sorted(extra))}")
        currency = pricing["currency"]
        if not isinstance(currency, str) or len(currency.strip()) != 3 or not currency.isalpha():
            raise ValueError("pricing currency must be a three-letter alphabetic code")
        normalized = {**pricing, "currency": currency.strip().upper()}
        rate_fields = {
            "input_per_million",
            "output_per_million",
            "cached_input_per_million",
        }
        for field in rate_fields & pricing.keys():
            try:
                rate = Decimal(str(pricing[field]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"pricing {field} must be numeric") from exc
            if not rate.is_finite() or rate < 0:
                raise ValueError(f"pricing {field} must be nonnegative and finite")
        return normalized


class RouteInput(BaseModel):
    model_id: str
    provider_model_id: str
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    allow_model_fallback: bool = False
    policy_id: str | None = None
    pool_id: str | None = None


class RoutingPolicyDefinition(BaseModel):
    health_weight: float = Field(default=3, ge=0)
    quota_weight: float = Field(default=2, ge=0)
    rate_limit_weight: float = Field(default=2, ge=0)
    concurrency_weight: float = Field(default=1, ge=0)
    latency_weight: float = Field(default=1, ge=0)
    failure_weight: float = Field(default=2, ge=0)
    min_quota_headroom: float = Field(default=0, ge=0, le=1)
    min_rpm_headroom: float = Field(default=0, ge=0, le=1)
    min_tpm_headroom: float = Field(default=0, ge=0, le=1)
    max_latency_ms: float | None = Field(default=None, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    allowed_credential_ids: list[str] = Field(default_factory=list)


class RoutingPolicyInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    policy: RoutingPolicyDefinition = Field(default_factory=RoutingPolicyDefinition)


async def _context(request: Request) -> tuple[AdminClaims, Any] | JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = getattr(request.app.state, "db_pool", None)
    pool = getattr(request.state, "control_plane_connection", pool)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    return claims, pool


async def _audit(
    pool: Any,
    claims: AdminClaims,
    action: str,
    resource_type: str,
    resource_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await pool.execute(
        """
        insert into public.audit_logs(actor_id, action, resource_type, resource_id, metadata)
        values($1, $2, $3, $4, $5::jsonb)
        """,
        claims.subject,
        action,
        resource_type,
        resource_id,
        json.dumps(metadata or {}),
    )


def _audit_fields(body: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    return {
        "changed_fields": sorted(body.model_fields_set - excluded),
    }


def _not_found(resource: str) -> JSONResponse:
    return JSONResponse({"error": f"{resource}_not_found"}, status_code=404)


def _masked_hint(secret: str) -> str:
    if len(secret) < 12:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


@router.post("/providers")
async def create_provider(request: Request, body: ProviderInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        insert into public.providers(
          name, provider_type, protocol, base_url, enabled, priority,
          capabilities, timeout_seconds, settings
        ) values($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
        returning id, name, provider_type, protocol, base_url, enabled,
                  priority, capabilities, timeout_seconds, settings, health
        """,
        body.name,
        body.provider_type,
        body.protocol,
        body.base_url,
        body.enabled,
        body.priority,
        body.capabilities,
        body.timeout_seconds,
        json.dumps(body.settings),
    )
    await _audit(
        pool, claims, "provider_created", "provider", str(row["id"]), _audit_fields(body)
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    request: Request,
    body: ProviderInput,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        update public.providers set
          name=$2, provider_type=coalesce($3, provider_type),
          protocol=coalesce($4, protocol), base_url=$5, enabled=$6,
          priority=$7, capabilities=$8, timeout_seconds=$9,
          settings=case when $10::boolean then $11::jsonb else settings end,
          updated_at=now()
        where id=$1
        returning id, name, provider_type, protocol, base_url, enabled,
                  priority, capabilities, timeout_seconds, settings, health
        """,
        provider_id,
        body.name,
        body.provider_type,
        body.protocol,
        body.base_url,
        body.enabled,
        body.priority,
        body.capabilities,
        body.timeout_seconds,
        "settings" in body.model_fields_set,
        json.dumps(body.settings),
    )
    if row is None:
        return _not_found("provider")
    await _audit(pool, claims, "provider_updated", "provider", provider_id, _audit_fields(body))
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.providers where id=$1 returning id",
        provider_id,
    )
    if row is None:
        return _not_found("provider")
    await _audit(pool, claims, "provider_deleted", "provider", provider_id)
    return JSONResponse({"deleted": True})


@router.post("/credentials")
async def create_credential(request: Request, body: CredentialInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    encryption_key = request.app.state.settings.credential_encryption_key
    if not encryption_key:
        return JSONResponse({"error": "credential_encryption_not_configured"}, status_code=503)
    credential_id = str(uuid4())
    envelope = CredentialCipher.from_base64(encryption_key).encrypt(
        body.secret,
        context=f"provider-credential:{credential_id}",
    )
    row = await pool.fetchrow(
        """
        insert into public.provider_credentials(
          id, provider_id, name, secret_version, secret_nonce,
           secret_ciphertext, masked_hint, priority, quota_limit, quota_threshold,
           requests_per_minute, tokens_per_minute
          ) values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        returning id, provider_id, name, masked_hint, enabled, priority,
                  health, quota_limit, quota_used, cooldown_until
        """,
        credential_id,
        body.provider_id,
        body.name,
        envelope.version,
        envelope.nonce,
        envelope.ciphertext,
        _masked_hint(body.secret),
        body.priority,
        body.quota_limit,
        body.quota_threshold,
        body.requests_per_minute,
        body.tokens_per_minute,
    )
    await _audit(
        pool, claims, "credential_created", "credential", credential_id,
        {"changed_fields": sorted(body.model_fields_set - {"secret"})},
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.post("/credentials/{credential_id}/rotate")
async def rotate_credential(
    credential_id: str,
    request: Request,
    body: CredentialRotationInput,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    encryption_key = request.app.state.settings.credential_encryption_key
    if not encryption_key:
        return JSONResponse({"error": "credential_encryption_not_configured"}, status_code=503)
    envelope = CredentialCipher.from_base64(encryption_key).encrypt(
        body.secret,
        context=f"provider-credential:{credential_id}",
    )
    row = await pool.fetchrow(
        """
        update public.provider_credentials set
          secret_version=$2, secret_nonce=$3, secret_ciphertext=$4, masked_hint=$5,
          health='healthy', cooldown_until=null, updated_at=now()
        where id=$1
        returning id, provider_id, name, masked_hint, enabled, priority,
                  health, quota_limit, quota_used, cooldown_until
        """,
        credential_id,
        envelope.version,
        envelope.nonce,
        envelope.ciphertext,
        _masked_hint(body.secret),
    )
    if row is None:
        return _not_found("credential")
    await _audit(pool, claims, "credential_rotated", "credential", credential_id, {})
    return JSONResponse(jsonable_encoder(dict(row)))


@router.put("/credentials/{credential_id}")
async def update_credential(
    credential_id: str,
    request: Request,
    body: CredentialUpdateInput,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        update public.provider_credentials set
          name=$2, enabled=$3, priority=$4, quota_limit=$5, quota_threshold=$6,
          requests_per_minute=$7, tokens_per_minute=$8, updated_at=now()
        where id=$1
        returning id, provider_id, name, masked_hint, enabled, priority,
                  health, quota_limit, quota_used, quota_threshold,
                  requests_per_minute, tokens_per_minute, cooldown_until
        """,
        credential_id,
        body.name,
        body.enabled,
        body.priority,
        body.quota_limit,
        body.quota_threshold,
        body.requests_per_minute,
        body.tokens_per_minute,
    )
    if row is None:
        return _not_found("credential")
    await _audit(
        pool, claims, "credential_updated", "credential", credential_id, _audit_fields(body)
    )
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.provider_credentials where id=$1 returning id",
        credential_id,
    )
    if row is None:
        return _not_found("credential")
    await _audit(pool, claims, "credential_deleted", "credential", credential_id)
    return JSONResponse({"deleted": True})


@router.post("/clients")
async def create_client(request: Request, body: ClientInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        insert into public.gateway_clients(
          name, allowed_protocols, allowed_models, enabled,
          requests_per_minute, tokens_per_minute, spending_limit
        ) values($1,$2,$3,$4,$5,$6,$7)
        returning id, name, allowed_protocols, allowed_models, enabled,
                  requests_per_minute, tokens_per_minute, spending_limit, created_at
        """,
        body.name,
        body.allowed_protocols,
        body.allowed_models,
        body.enabled,
        body.requests_per_minute,
        body.tokens_per_minute,
        body.spending_limit,
    )
    await _audit(
        pool, claims, "client_created", "client", str(row["id"]), _audit_fields(body)
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/clients/{client_id}")
async def update_client(
    client_id: str,
    request: Request,
    body: ClientInput,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        update public.gateway_clients set
          name=$2, allowed_protocols=$3, allowed_models=$4, enabled=$5, updated_at=now()
          , requests_per_minute=$6, tokens_per_minute=$7, spending_limit=$8
        where id=$1
        returning id, name, allowed_protocols, allowed_models, enabled,
                  requests_per_minute, tokens_per_minute, spending_limit,
                  created_at, updated_at
        """,
        client_id,
        body.name,
        body.allowed_protocols,
        body.allowed_models,
        body.enabled,
        body.requests_per_minute,
        body.tokens_per_minute,
        body.spending_limit,
    )
    if row is None:
        return _not_found("client")
    await _audit(pool, claims, "client_updated", "client", client_id, _audit_fields(body))
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/clients/{client_id}")
async def delete_client(client_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.gateway_clients where id=$1 returning id", client_id
    )
    if row is None:
        return _not_found("client")
    await _audit(pool, claims, "client_deleted", "client", client_id)
    return JSONResponse({"deleted": True})


@router.post("/clients/{client_id}/keys")
async def create_client_key(
    client_id: str,
    request: Request,
    body: ClientKeyInput | None = None,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    pepper = request.app.state.settings.key_pepper
    if not pepper:
        return JSONResponse({"error": "key_pepper_not_configured"}, status_code=503)
    exists = await pool.fetchval(
        "select 1 from public.gateway_clients where id=$1 and enabled",
        client_id,
    )
    if exists is None:
        return _not_found("client")
    key_id = str(uuid4())
    issued = GatewayKeyHasher.from_base64(pepper).issue(
        key_id=key_id,
        client_id=client_id,
    )
    await pool.execute(
        """
        insert into public.gateway_client_keys(
          id,client_id,key_prefix,key_digest,label,expires_at)
        values($1,$2,$3,$4,$5,$6)
        """,
        key_id,
        client_id,
        issued.record.key_prefix,
        issued.record.digest,
        body.label if body else None,
        body.expires_at if body else None,
    )
    await _audit(
        pool, claims, "gateway_key_created", "client_key", key_id,
        {"client_id": client_id},
    )
    return JSONResponse(
        {
            "id": key_id,
            "client_id": client_id,
            "key_prefix": issued.record.key_prefix,
            "key": issued.plaintext,
            "label": body.label if body else None,
            "expires_at": body.expires_at if body else None,
        },
        status_code=201,
    )


@router.get("/clients/{client_id}/keys")
async def client_keys(client_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch(
        """
        select id,client_id,key_prefix,label,enabled,last_used_at,expires_at,created_at,
               revoked_at,revoke_reason
        from public.gateway_client_keys where client_id=$1 order by created_at desc
        """,
        client_id,
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.post("/client-keys/{key_id}/revoke")
async def revoke_client_key(
    key_id: str,
    request: Request,
    body: KeyRevocationInput | None = None,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        update public.gateway_client_keys set enabled=false,revoked_at=coalesce(revoked_at,now()),
          revoke_reason=coalesce($2,revoke_reason)
        where id=$1 returning id,client_id,key_prefix,enabled,revoked_at,revoke_reason
        """,
        key_id,
        body.reason if body else None,
    )
    if row is None:
        return _not_found("client_key")
    await _audit(pool, claims, "gateway_key_revoked", "client_key", key_id)
    return JSONResponse(jsonable_encoder(dict(row)))


@router.post("/client-keys/{key_id}/rotate")
async def rotate_client_key(
    key_id: str,
    request: Request,
    body: ClientKeyInput | None = None,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    current = await pool.fetchrow(
        """select client_id,label,expires_at from public.gateway_client_keys
           where id=$1 and enabled and revoked_at is null""",
        key_id,
    )
    if current is None:
        return _not_found("client_key")
    pepper = request.app.state.settings.key_pepper
    if not pepper:
        return JSONResponse({"error": "key_pepper_not_configured"}, status_code=503)
    replacement_id = str(uuid4())
    issued = GatewayKeyHasher.from_base64(pepper).issue(
        key_id=replacement_id, client_id=str(current["client_id"])
    )
    await pool.execute(
        """insert into public.gateway_client_keys(
             id,client_id,key_prefix,key_digest,label,expires_at)
           values($1,$2,$3,$4,$5,$6)""",
        replacement_id, current["client_id"], issued.record.key_prefix, issued.record.digest,
        body.label if body else current["label"],
        body.expires_at if body else current["expires_at"],
    )
    await pool.execute(
        """update public.gateway_client_keys set enabled=false,revoked_at=now(),
             revoke_reason='rotated' where id=$1""",
        key_id,
    )
    await _audit(
        pool, claims, "gateway_key_rotated", "client_key", replacement_id,
        {"replaced_key_id": key_id, "client_id": str(current["client_id"])},
    )
    return JSONResponse(
        {
            "id": replacement_id,
            "client_id": str(current["client_id"]),
            "key_prefix": issued.record.key_prefix,
            "key": issued.plaintext,
            "label": body.label if body else current["label"],
            "expires_at": body.expires_at if body else current["expires_at"],
        },
        status_code=201,
    )


@router.post("/models")
async def create_model(request: Request, body: ModelInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    await pool.execute(
        """
        insert into public.models(id,display_name,capabilities,enabled,context_window)
        values($1,$2,$3,$4,$5)
        """,
        body.id, body.display_name, body.capabilities, body.enabled, body.context_window,
    )
    for alias in body.aliases:
        await pool.execute(
            "insert into public.model_aliases(alias,model_id) values($1,$2)",
            alias, body.id,
        )
    await _audit(pool, claims, "model_created", "model", body.id, _audit_fields(body))
    return JSONResponse({"id": body.id, "aliases": body.aliases}, status_code=201)


@router.put("/models/{model_id}")
async def update_model(model_id: str, request: Request, body: ModelInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    if body.id != model_id:
        return JSONResponse({"error": "model_id_is_immutable"}, status_code=422)
    row = await pool.fetchrow(
        """update public.models set display_name=$2,capabilities=$3,enabled=$4,
                  context_window=$5,updated_at=now() where id=$1
           returning id,display_name,capabilities,enabled,context_window""",
        model_id, body.display_name, body.capabilities, body.enabled, body.context_window,
    )
    if row is None:
        return _not_found("model")
    await pool.execute("delete from public.model_aliases where model_id=$1", model_id)
    for alias in body.aliases:
        await pool.execute(
            "insert into public.model_aliases(alias,model_id) values($1,$2)", alias, model_id
        )
    await _audit(pool, claims, "model_updated", "model", model_id, _audit_fields(body))
    return JSONResponse(jsonable_encoder({**dict(row), "aliases": body.aliases}))


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow("delete from public.models where id=$1 returning id", model_id)
    if row is None:
        return _not_found("model")
    await _audit(pool, claims, "model_deleted", "model", model_id)
    return JSONResponse({"deleted": True})


@router.post("/provider-models")
async def create_provider_model(
    request: Request,
    body: ProviderModelInput,
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    provider = await pool.fetchrow(
        "select provider_type from public.providers where id=$1 and enabled",
        body.provider_id,
    )
    if provider is None:
        return _not_found("provider")
    row = await pool.fetchrow(
        """
        insert into public.provider_models(
          provider_id,model_id,upstream_model_id,protocol,
          capabilities,priority,weight,enabled,max_concurrency,settings,pricing
        ) values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb)
        returning id,provider_id,model_id,upstream_model_id,protocol,
                  capabilities,priority,weight,enabled,max_concurrency,settings,pricing
        """,
        body.provider_id,
        body.model_id,
        body.upstream_model_id,
        body.protocol,
        body.capabilities,
        body.priority,
        body.weight,
        body.enabled,
        body.max_concurrency,
        json.dumps(body.settings),
        json.dumps(body.pricing),
    )
    await _audit(
        pool, claims, "provider_model_created", "provider_model", str(row["id"]),
        _audit_fields(body),
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/provider-models/{provider_model_id}")
async def update_provider_model(
    provider_model_id: str, request: Request, body: ProviderModelInput
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    provider = await pool.fetchrow(
        "select provider_type from public.providers where id=$1 and enabled",
        body.provider_id,
    )
    if provider is None:
        return _not_found("provider")
    row = await pool.fetchrow(
        """update public.provider_models set provider_id=$2,model_id=$3,upstream_model_id=$4,
                   protocol=$5,capabilities=$6,priority=$7,weight=$8,enabled=$9,
                   max_concurrency=$10,settings=$11::jsonb,pricing=$12::jsonb,updated_at=now()
           where id=$1 returning id,provider_id,model_id,upstream_model_id,protocol,
                                  capabilities,priority,weight,enabled,max_concurrency,settings,pricing""",
        provider_model_id, body.provider_id, body.model_id, body.upstream_model_id,
        body.protocol, body.capabilities, body.priority, body.weight, body.enabled,
          body.max_concurrency, json.dumps(body.settings), json.dumps(body.pricing),
    )
    if row is None:
        return _not_found("provider_model")
    await _audit(
        pool, claims, "provider_model_updated", "provider_model", provider_model_id,
        _audit_fields(body),
    )
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/provider-models/{provider_model_id}")
async def delete_provider_model(provider_model_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.provider_models where id=$1 returning id", provider_model_id
    )
    if row is None:
        return _not_found("provider_model")
    await _audit(pool, claims, "provider_model_deleted", "provider_model", provider_model_id)
    return JSONResponse({"deleted": True})


@router.post("/routes")
async def create_route(request: Request, body: RouteInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """
        insert into public.model_routes(
          model_id,provider_model_id,priority,enabled,allow_model_fallback,policy_id,pool_id
        ) values($1,$2,$3,$4,$5,$6,$7)
        returning id,model_id,provider_model_id,priority,enabled,allow_model_fallback,
                  policy_id,pool_id
        """,
        body.model_id,
        body.provider_model_id,
        body.priority,
        body.enabled,
        body.allow_model_fallback,
        body.policy_id,
        body.pool_id,
    )
    await _audit(
        pool, claims, "route_created", "model_route", str(row["id"]), _audit_fields(body)
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/routes/{route_id}")
async def update_route(route_id: str, request: Request, body: RouteInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.model_routes set model_id=$2,provider_model_id=$3,priority=$4,
                  enabled=$5,allow_model_fallback=$6,policy_id=$7,pool_id=$8 where id=$1
           returning id,model_id,provider_model_id,priority,enabled,
                     allow_model_fallback,policy_id,pool_id""",
        route_id, body.model_id, body.provider_model_id, body.priority,
        body.enabled, body.allow_model_fallback, body.policy_id, body.pool_id,
    )
    if row is None:
        return _not_found("route")
    await _audit(pool, claims, "route_updated", "model_route", route_id, _audit_fields(body))
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/routes/{route_id}")
async def delete_route(route_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow("delete from public.model_routes where id=$1 returning id", route_id)
    if row is None:
        return _not_found("route")
    await _audit(pool, claims, "route_deleted", "model_route", route_id)
    return JSONResponse({"deleted": True})


@router.post("/routing-policies")
async def create_routing_policy(request: Request, body: RoutingPolicyInput) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """insert into public.routing_policies(name,enabled,policy) values($1,$2,$3::jsonb)
           returning id,name,enabled,policy,created_at,updated_at""",
        body.name, body.enabled, json.dumps(body.policy.model_dump()),
    )
    await _audit(
        pool, claims, "routing_policy_created", "routing_policy", str(row["id"]),
        _audit_fields(body),
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/routing-policies/{policy_id}")
async def update_routing_policy(
    policy_id: str, request: Request, body: RoutingPolicyInput
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.routing_policies set name=$2,enabled=$3,policy=$4::jsonb,
                  updated_at=now() where id=$1
           returning id,name,enabled,policy,created_at,updated_at""",
        policy_id, body.name, body.enabled, json.dumps(body.policy.model_dump()),
    )
    if row is None:
        return _not_found("routing_policy")
    await _audit(
        pool, claims, "routing_policy_updated", "routing_policy", policy_id, _audit_fields(body)
    )
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/routing-policies/{policy_id}")
async def delete_routing_policy(policy_id: str, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.routing_policies where id=$1 returning id", policy_id
    )
    if row is None:
        return _not_found("routing_policy")
    await _audit(pool, claims, "routing_policy_deleted", "routing_policy", policy_id)
    return JSONResponse({"deleted": True})


async def _rows(pool: Any, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch(query)]


async def _snapshot_payload(pool: Any) -> dict[str, Any]:
    clients = await _rows(
        pool,
        """select id::text,name,enabled,allowed_protocols,allowed_models,
                  requests_per_minute,tokens_per_minute,spending_limit
           from public.gateway_clients""",
    )
    keys = await _rows(
        pool,
        """select id::text,client_id::text,key_prefix,key_digest,enabled,expires_at
           from public.gateway_client_keys where revoked_at is null""",
    )
    providers = await _rows(
        pool,
         """select id::text,name,provider_type,protocol,base_url,enabled,priority,
                   capabilities,timeout_seconds,health,
                   coalesce(settings->'default_headers','{}'::jsonb) as default_headers,
                   coalesce(settings->'required_betas','[]'::jsonb) as required_betas,
                   coalesce(settings->>'auth_scheme','default') as auth_scheme,
                   coalesce(settings->'endpoint_query','{}'::jsonb) as endpoint_query
            from public.providers""",
    )
    credentials = await _rows(
        pool,
        """select id::text,provider_id::text,secret_version,secret_nonce,
                   secret_ciphertext,enabled,priority,health,quota_limit,
                   quota_used,cooldown_until,requests_per_minute,tokens_per_minute,
                   coalesce(
                     array_agg(cma.provider_model_id::text)
                       filter (where cma.provider_model_id is not null),
                     '{}'
                   ) as supported_provider_model_ids
            from public.provider_credentials pc
            left join public.credential_model_access cma on cma.credential_id=pc.id
            group by pc.id""",
    )
    models = await _rows(
        pool,
        "select id,enabled,capabilities from public.models",
    )
    aliases = await pool.fetch(
        "select alias,model_id from public.model_aliases order by model_id,alias"
    )
    provider_models = await _rows(
        pool,
        """select pm.id::text,r.id::text as route_id,
                   pm.model_id as canonical_model_id,pm.provider_id::text,
                   pm.upstream_model_id,pm.protocol,pm.capabilities,r.priority,pm.weight,
                     pm.max_concurrency,
                     case when pm.settings ? 'default_headers'
                          then pm.settings->'default_headers' end as default_headers,
                     case when pm.settings ? 'required_betas'
                          then pm.settings->'required_betas' end as required_betas,
                     case when pm.settings ? 'auth_scheme'
                          then pm.settings->>'auth_scheme' end as auth_scheme,
                     case when pm.settings ? 'endpoint_query'
                     then pm.settings->'endpoint_query' end as endpoint_query,
                    pm.pricing,r.allow_model_fallback,r.pool_id::text as pool_id,
                     coalesce(pp.enabled,false) as pool_enabled,
                     case when r.pool_id is null then null else array(
                       select ppm.credential_id::text
                       from public.provider_pool_members ppm
                       where ppm.pool_id=r.pool_id and ppm.provider_model_id=pm.id
                         and ppm.enabled and not ppm.draining
                       order by ppm.priority,ppm.credential_id
                      ) end as active_pool_credential_ids,
                     case when r.pool_id is null then null else coalesce((
                        select jsonb_object_agg(ppm.credential_id::text,
                          jsonb_build_object('priority',ppm.priority,'weight',ppm.weight))
                        from public.provider_pool_members ppm
                        where ppm.pool_id=r.pool_id and ppm.provider_model_id=pm.id
                          and ppm.enabled and not ppm.draining
                       ),'{}'::jsonb) end as active_pool_members,
                     case when pp.enabled then pp.strategy end as pool_strategy,
                     (pm.enabled and r.enabled) as enabled,
                     case when rp.enabled then rp.policy else null end as routing_policy
           from public.provider_models pm
            join public.model_routes r
              on r.provider_model_id = pm.id and r.model_id = pm.model_id
            left join public.routing_policies rp on rp.id = r.policy_id
            left join public.provider_pools pp on pp.id = r.pool_id
            where pm.enabled and r.enabled""",
    )
    alias_map: dict[str, list[str]] = {}
    for row in aliases:
        alias_map.setdefault(row["model_id"], []).append(row["alias"])
    for row in models:
        row["aliases"] = alias_map.get(row["id"], [])
    for row in credentials:
        row["quota_limit"] = float(row["quota_limit"]) if row["quota_limit"] else None
        row["quota_used"] = float(row["quota_used"])
    for row in providers + provider_models:
        for field in ("default_headers", "required_betas", "endpoint_query"):
            if isinstance(row.get(field), str):
                row[field] = json.loads(row[field])
    for collection in (providers, models, provider_models):
        for row in collection:
            row["capabilities"] = [
                capability.strip() for capability in row.get("capabilities") or []
            ]
    for row in provider_models:
        pool_id = row.pop("pool_id", None)
        pool_enabled = row.pop("pool_enabled", False)
        active_credentials = row.pop("active_pool_credential_ids", None)
        active_members = row.pop("active_pool_members", None)
        row["allowed_credential_ids"] = (
            None if pool_id is None else active_credentials if pool_enabled else []
        )
        row["pool_members"] = (
            None if pool_id is None else active_members if pool_enabled else {}
        )
        if isinstance(row.get("routing_policy"), str):
            row["routing_policy"] = json.loads(row["routing_policy"])
        if isinstance(row.get("pricing"), str):
            row["pricing"] = json.loads(row["pricing"])
    return {
        "clients": clients,
        "gateway_keys": keys,
        "providers": providers,
        "credentials": credentials,
        "models": models,
        "provider_models": provider_models,
    }


@router.get("/config/status")
async def config_status(request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    payload = await _snapshot_payload(pool)
    RuntimeSnapshot.model_validate(payload)
    working_checksum = configuration_checksum(payload)
    published = await pool.fetchrow(
        """select id,checksum,payload,published_at from public.config_versions
           where status='published'"""
    )
    published_payload = published["payload"] if published else {}
    if isinstance(published_payload, str):
        published_payload = json.loads(published_payload)
    section_names = {
        "providers": "Providers",
        "credentials": "Credentials",
        "models": "Models",
        "provider_models": "Mappings and routes",
        "clients": "Gateway clients",
    }
    working_projection = configuration_projection(payload)
    published_projection = configuration_projection(published_payload)
    changed_sections = [
        label for key, label in section_names.items()
        if working_projection.get(key, []) != published_projection.get(key, [])
    ]
    published_checksum = published["checksum"] if published else None
    has_changes = published is None or published_checksum != working_checksum
    if published is not None and published_checksum != working_checksum:
        has_changes = working_projection != published_projection
    return JSONResponse(jsonable_encoder({
        "active_version": published["id"] if published else None,
        "active_checksum": published["checksum"] if published else None,
        "working_checksum": working_checksum,
        "has_unpublished_changes": has_changes,
        "changed_sections": changed_sections if has_changes else [],
        "published_at": published["published_at"] if published else None,
    }))


@router.post("/config/publish")
async def publish_config(request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    async with pool.acquire() as connection, connection.transaction(
        isolation="repeatable_read"
    ):
        await connection.execute("select pg_advisory_xact_lock(hashtext('config_publication'))")
        payload = await _snapshot_payload(connection)
        try:
            RuntimeSnapshot.model_validate(payload)
        except ValidationError as exc:
            errors = [
                {
                    "location": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                }
                for error in exc.errors(include_input=False)
            ]
            return JSONResponse(
                {"error": "configuration_validation_failed", "details": errors},
                status_code=422,
            )
        checksum = configuration_checksum(payload)
        await connection.execute(
            "update public.config_versions set status='superseded' where status='published'"
        )
        row = await connection.fetchrow(
            """
            insert into public.config_versions(
              status,schema_version,payload,checksum,created_by,published_at
            ) values('published',1,$1::jsonb,$2,$3,now())
            returning id,published_at
            """,
            json.dumps(payload, default=str),
            checksum,
            claims.subject,
        )
        await _audit(
            connection, claims, "config_published", "config_version", str(row["id"]),
            {"checksum": checksum, "schema_version": 1},
        )
    return JSONResponse(
        jsonable_encoder(
            {"version": row["id"], "checksum": checksum, "published_at": row["published_at"]}
        ),
        status_code=201,
    )


@router.get("/config/versions")
async def config_versions(
    request: Request,
    limit: int = Query(default=25, ge=1, le=100),
) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch(
        """select id,status,schema_version,checksum,created_by,created_at,published_at
           from public.config_versions order by id desc limit $1""",
        limit,
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.post("/config/versions/{version}/rollback")
async def rollback_config(version: int, request: Request) -> JSONResponse:
    context = await _context(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("select pg_advisory_xact_lock(hashtext('config_publication'))")
        selected = await connection.fetchrow(
            """select id,schema_version,payload,checksum
               from public.config_versions where id=$1""",
            version,
        )
        if selected is None:
            return _not_found("config_version")
        payload = selected["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            RuntimeSnapshot.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                {
                    "error": "configuration_validation_failed",
                    "details": [
                        {
                            "location": ".".join(str(part) for part in error["loc"]),
                            "message": error["msg"],
                        }
                        for error in exc.errors(include_input=False)
                    ],
                },
                status_code=422,
            )
        checksum = configuration_checksum(payload)
        if selected["checksum"] not in {
            checksum,
            legacy_configuration_checksum(payload),
            legacy_checksum(payload),
        }:
            return JSONResponse(
                {"error": "configuration_checksum_invalid"}, status_code=409
            )
        await connection.execute(
            "update public.config_versions set status='superseded' where status='published'"
        )
        published = await connection.fetchrow(
            """insert into public.config_versions(
                 status,schema_version,payload,checksum,created_by,published_at
               ) values('published',$1,$2::jsonb,$3,$4,now())
               returning id,published_at""",
            selected["schema_version"],
            json.dumps(payload, default=str),
            checksum,
            claims.subject,
        )
        await _audit(
            connection,
            claims,
            "config_rolled_back",
            "config_version",
            str(published["id"]),
            {
                "source_version": version,
                "checksum": checksum,
                "schema_version": selected["schema_version"],
            },
        )
    return JSONResponse(
        jsonable_encoder(
            {
                "version": published["id"],
                "source_version": version,
                "status": "published",
                "published_at": published["published_at"],
            }
        )
    )
