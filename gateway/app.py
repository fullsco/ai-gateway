import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from gateway import __version__
from gateway.admin.api import router as admin_router
from gateway.admin.auth import SupabaseJWTVerifier
from gateway.admin.control_plane import router as control_plane_router
from gateway.admin.operations import router as operations_router
from gateway.api.messages import router as messages_router
from gateway.api.models import router as models_router
from gateway.api.openai import router as openai_router
from gateway.config import Settings, get_settings
from gateway.configuration import (
    CachedConfiguration,
    PostgresSnapshotRepository,
    RuntimeBuilder,
    RuntimeManager,
    create_pool,
)
from gateway.context import get_request_id
from gateway.health.probes import health_probe_loop
from gateway.logging import configure_logging, log_event
from gateway.middleware import RequestContextMiddleware
from gateway.observability import PassiveHealthRecorder
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.lifecycle")


async def _refresh_runtime(app: FastAPI, manager: RuntimeManager) -> None:
    while True:
        await asyncio.sleep(app.state.settings.config_refresh_seconds)
        try:
            app.state.runtime = await manager.refresh()
            app.state.ready = app.state.runtime is not None
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "runtime_refresh_failed",
                error_type=type(exc).__name__,
            )


def create_app(
    settings: Settings | None = None,
    runtime: GatewayRuntime | None = None,
    *,
    admin_verifier: SupabaseJWTVerifier | None = None,
    db_pool=None,
) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ready = app_settings.environment in {"development", "test"} or runtime is not None
        log_event(logger, logging.INFO, "gateway_started", environment=app_settings.environment)
        refresh_task: asyncio.Task[None] | None = None
        pool = None
        manager = None
        health_recorder = None
        probe_task = None
        if app_settings.database_url:
            try:
                if not app_settings.credential_encryption_key or not app_settings.key_pepper:
                    raise ValueError(
                        "Database runtime requires credential_encryption_key and key_pepper"
                    )
                pool = await create_pool(app_settings.database_url)
                app.state.db_pool = pool
                manager = RuntimeManager(
                    CachedConfiguration(PostgresSnapshotRepository(pool)),
                    RuntimeBuilder(
                        encryption_key=app_settings.credential_encryption_key,
                        key_pepper=app_settings.key_pepper,
                    ),
                )
                app.state.runtime_manager = manager
                refresh_task = asyncio.create_task(_refresh_runtime(app, manager))
                try:
                    app.state.runtime = await manager.refresh()
                except Exception as exc:
                    app.state.runtime = runtime
                    log_event(
                        logger,
                        logging.ERROR,
                        "runtime_initialization_failed",
                        error_type=type(exc).__name__,
                    )
                app.state.ready = app.state.runtime is not None
            except Exception as exc:
                app.state.runtime = runtime
                app.state.ready = runtime is not None
                log_event(
                    logger,
                    logging.ERROR,
                    "runtime_initialization_failed",
                    error_type=type(exc).__name__,
                )
        active_pool = getattr(app.state, "db_pool", None)
        if active_pool is not None:
            health_recorder = PassiveHealthRecorder(active_pool)
            health_recorder.start()
            app.state.health_recorder = health_recorder
        if app_settings.health_probe_enabled:
            probe_task = asyncio.create_task(
                health_probe_loop(
                    app_settings.health_probe_interval_seconds,
                    lambda: getattr(app.state, "runtime", None),
                    lambda: getattr(app.state, "health_recorder", None),
                )
            )
        if app.state.admin_verifier is None and app_settings.supabase_url:
            app.state.admin_verifier = SupabaseJWTVerifier(
                app_settings.supabase_url,
                audience=app_settings.supabase_jwt_audience,
                admin_role=app_settings.admin_role,
            )
        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
            if probe_task is not None:
                probe_task.cancel()
                with suppress(asyncio.CancelledError):
                    await probe_task
            if health_recorder is not None:
                await health_recorder.close()
            if manager is not None:
                await manager.close()
            if pool is not None:
                await pool.close()
            app.state.ready = False
            log_event(logger, logging.INFO, "gateway_stopped")

    app = FastAPI(
        title="AI Gateway",
        version=__version__,
        docs_url="/docs" if app_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.runtime = runtime
    app.state.db_pool = db_pool
    app.state.admin_verifier = admin_verifier
    app.state.health_recorder = None
    app.state.ready = False
    app.add_middleware(RequestContextMiddleware, settings=app_settings)

    @app.middleware("http")
    async def control_plane_mutation_transaction(request: Request, call_next):
        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        is_control_plane = request.url.path.startswith("/api/admin/v1/")
        is_snapshot_operation = "/config/" in request.url.path
        pool = getattr(request.app.state, "db_pool", None)
        transactional = (
            is_mutation
            and is_control_plane
            and not is_snapshot_operation
            and pool is not None
        )
        if not transactional:
            return await call_next(request)
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
    app.include_router(messages_router)
    app.include_router(models_router)
    app.include_router(openai_router)
    app.include_router(admin_router)
    app.include_router(control_plane_router)
    app.include_router(operations_router)

    @app.get("/health", include_in_schema=False)
    @app.head("/health", include_in_schema=False)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/ready", include_in_schema=False)
    @app.head("/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        if not request.app.state.ready:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, str]:
        return {"version": __version__}

    @app.exception_handler(Exception)
    async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
        log_event(logger, logging.ERROR, "unhandled_exception", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "message": "The gateway encountered an internal error.",
                    "request_id": get_request_id(),
                }
            },
        )

    return app


app = create_app()
