import base64
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI, Request

from gateway.admin.auth import AdminClaims
from gateway.admin.reconcile import router
from gateway.config import Settings


class AdminVerifier:
    async def verify(self, token: str) -> AdminClaims:
        assert token == "admin-session"
        return AdminClaims(
            subject="00000000-0000-0000-0000-000000000001",
            email=None,
            role="admin",
        )


class Transaction:
    def __init__(self) -> None:
        self.outcome: str | None = None

    async def start(self) -> None:
        self.outcome = "started"

    async def commit(self) -> None:
        self.outcome = "committed"

    async def rollback(self) -> None:
        self.outcome = "rolled_back"


class ConnectionContext(AbstractAsyncContextManager[Any]):
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class ReconcilePool:
    def __init__(
        self,
        *,
        alias_rows: list[dict[str, Any]] | None = None,
        namespace_model_rows: list[dict[str, Any]] | None = None,
        shared_model_rows: list[dict[str, Any]] | None = None,
        conflicting_pool_provider: str | None = None,
        existing_provider_id: str | None = None,
        topology_conflict: str | None = None,
        previously_mapped_models: list[str] | None = None,
        sync_disabled_ids: list[str] | None = None,
        sync_enabled_ids: list[str] | None = None,
    ) -> None:
        self.alias_rows = alias_rows or []
        self.namespace_model_rows = namespace_model_rows or []
        self.shared_model_rows = shared_model_rows or []
        self.conflicting_pool_provider = conflicting_pool_provider
        self.existing_provider_id = existing_provider_id
        self.topology_conflict = topology_conflict
        self.previously_mapped_models = previously_mapped_models or []
        self.sync_disabled_ids = sync_disabled_ids or []
        self.sync_enabled_ids = sync_enabled_ids or []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions: list[Transaction] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        if "m.display_name" in query and "from public.models" in query:
            return self.shared_model_rows
        if "from public.model_aliases" in query:
            return self.alias_rows
        if (
            "update public.models m set enabled=false" in query
            or "update public.models m set enabled=true" in query
        ):
            source = (
                self.sync_disabled_ids
                if "enabled=false" in query
                else self.sync_enabled_ids
            )
            return [{"id": model_id} for model_id in source]
        if "select distinct model_id from public.provider_models" in query:
            return [{"model_id": model_id} for model_id in self.previously_mapped_models]
        if "from public.models" in query:
            return self.namespace_model_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "insert into public.providers" in query:
            return {"id": "provider-id"}
        if "from public.provider_credentials" in query:
            return None
        if "insert into public.provider_models" in query:
            return {"id": "mapping-id"}
        if "insert into public.provider_pools" in query:
            return {"id": "pool-id"}
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "gen_random_uuid" in query:
            return "credential-id"
        if "select id::text from public.providers" in query:
            return self.existing_provider_id
        if "with provider_credentials" in query:
            return self.topology_conflict
        if "from public.provider_pools" in query:
            return self.conflicting_pool_provider
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))

    def acquire(self) -> ConnectionContext:
        return ConnectionContext(self)

    def transaction(self, **options: Any) -> Transaction:
        transaction = Transaction()
        self.transactions.append(transaction)
        return transaction


def app_settings(encryption_key: str | None = None) -> Settings:
    return Settings(
        environment="test",
        credential_encryption_key=(
            base64.b64encode(b"e" * 32).decode() if encryption_key is None else encryption_key
        ),
        key_pepper=base64.b64encode(b"p" * 32).decode(),
        _env_file=None,
    )


def auth() -> dict[str, str]:
    return {"authorization": "Bearer admin-session"}


def reconcile_app(pool: ReconcilePool, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings or app_settings()
    app.state.admin_verifier = AdminVerifier()
    app.state.db_pool = pool

    @app.middleware("http")
    async def control_plane_mutation_transaction(request: Request, call_next):
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            request.state.control_plane_connection = connection
            try:
                response = await call_next(request)
            except BaseException:
                await transaction.rollback()
                raise
            if response.status_code >= 400:
                await transaction.rollback()
            else:
                await transaction.commit()
            return response

    app.include_router(router)
    return app


def reconcile_payload(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Provider",
        "base_url": "https://provider.example",
        "models": [
            {
                "id": "model-1",
                "display_name": "Model 1",
                "aliases": ["latest"],
                "capabilities": ["streaming"],
            }
        ],
        "mappings": [
            {
                "model_id": "model-1",
                "upstream_model_id": "upstream-1",
                "protocol": "anthropic_messages",
                "capabilities": ["streaming"],
            }
        ],
        "routes": [
            {
                "model_id": "model-1",
                "mapping_upstream_model_id": "upstream-1",
                "mapping_protocol": "anthropic_messages",
            }
        ],
    }
    payload.update(updates)
    return payload
