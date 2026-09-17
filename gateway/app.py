import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from gateway import __version__
from gateway.accounting_worker import accounting_loop
from gateway.admin.accounting import router as accounting_router
from gateway.admin.api import router as admin_router
from gateway.admin.auth import SupabaseJWTVerifier
from gateway.admin.control_plane import router as control_plane_router
from gateway.admin.metrics import router as metrics_router
from gateway.admin.operations import router as operations_router
from gateway.admin.reconcile import router as reconcile_router
from gateway.admission import InferenceAdmissionMiddleware
from gateway.alerts_monitor import alert_monitor_loop
from gateway.api.artifacts import router as artifacts_router
from gateway.api.messages import router as messages_router
from gateway.api.models import router as models_router
from gateway.api.openai import router as openai_router
from gateway.api.workflows import router as workflows_router
from gateway.config import Settings, get_settings
from gateway.configuration import (
    CachedConfiguration,
    PostgresSnapshotRepository,
    RuntimeBuilder,
    RuntimeManager,
    create_pool,
)
from gateway.context import get_request_id
from gateway.dependency_health import DependencyHealth, requires_database
from gateway.draining import DrainState
from gateway.health.limits import HealthProbeLimiter, ProbeLimitConfig
from gateway.health.probes import health_probe_loop
from gateway.health.usage import usage_poll_loop
from gateway.lifecycle import managed_lifespan
from gateway.logging import configure_logging, log_event
from gateway.metrics import sample_event_loop
from gateway.middleware import RequestContextMiddleware
from gateway.observability import PassiveHealthRecorder
from gateway.routing.live_state import LiveOperationalState, QuotaPolicy
from gateway.runtime import GatewayRuntime
from gateway.schema_checks import require_circuit_schema, require_execution_schema

logger = logging.getLogger("gateway.lifecycle")


async def _refresh_live_state(interval: float, live_state: LiveOperationalState) -> None:
    """Keep dynamic operational state fresh without touching configuration."""
    while True:
        await asyncio.sleep(interval)
        try:
            await live_state.refresh()
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.WARNING,
                "live_state_refresh_failed",
                error_type=type(exc).__name__,
            )


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

    @managed_lifespan
    async def lifespan(app: FastAPI):
        resources = app.state.lifecycle_resources
        resources.task(sample_event_loop())
        app.state.ready = app_settings.environment in {"development", "test"} or runtime is not None
        log_event(logger, logging.INFO, "gateway_started", environment=app_settings.environment)
        pool = None
        manager = None
        health_recorder = None
        health_probe_limiter = None
        if app_settings.database_url:
            try:
                if not app_settings.credential_encryption_key or not app_settings.key_pepper:
                    raise ValueError(
                        "Database runtime requires credential_encryption_key and key_pepper"
                    )
                pool = resources.resource(await create_pool(app_settings.database_url))
                app.state.db_pool = pool
                manager = RuntimeManager(
                    CachedConfiguration(PostgresSnapshotRepository(pool)),
                    RuntimeBuilder(
                        encryption_key=app_settings.credential_encryption_key,
                        key_pepper=app_settings.key_pepper,
                    ),
                )
                resources.resource(manager)
                app.state.runtime_manager = manager
                resources.task(_refresh_runtime(app, manager))
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
        resources.resource(app.state.dependency_health)
        required_tables = []
        if app_settings.workflows_enabled or app_settings.service_role == "workflow":
            if not app_settings.durable_accounting_enabled:
                raise RuntimeError("Workflow execution requires durable accounting")
            required_tables.extend(("workflow_jobs", "workflow_events", "capacity_leases"))
        if app_settings.distributed_admission_enabled:
            required_tables.extend(("capacity_leases", "route_circuits"))
        for table in required_tables:
            if active_pool is None or not await active_pool.fetchval(
                "select to_regclass($1)", f"public.{table}"
            ):
                raise RuntimeError("Required workflow/coordination migration is missing")
        if app_settings.durable_accounting_enabled:
            if active_pool is None:
                raise RuntimeError("Durable accounting requires a database")
            if not await active_pool.fetchval("select to_regclass('public.settlement_outbox')"):
                raise RuntimeError("Durable accounting migration is required")
            await require_execution_schema(active_pool)
            if app_settings.service_role in {"combined", "maintenance"}:
                resources.task(accounting_loop(active_pool, settings=app_settings))
        if app_settings.distributed_admission_enabled:
            await require_circuit_schema(active_pool)
        if active_pool is not None:
            health_recorder = PassiveHealthRecorder(active_pool)
            resources.resource(health_recorder)
            health_recorder.start()
            app.state.health_recorder = health_recorder
            health_probe_limiter = HealthProbeLimiter(
                active_pool,
                ProbeLimitConfig(
                    daily_limit=app_settings.health_probe_daily_limit,
                    min_interval_seconds=app_settings.health_probe_min_interval_seconds,
                    lease_seconds=app_settings.health_probe_lease_seconds,
                    failure_backoff_seconds=app_settings.health_probe_failure_backoff_seconds,
                    max_backoff_seconds=app_settings.health_probe_max_backoff_seconds,
                    manual_daily_limit=app_settings.health_probe_manual_daily_limit,
                    manual_min_interval_seconds=(
                        app_settings.health_probe_manual_min_interval_seconds
                    ),
                ),
            )
            app.state.health_probe_limiter = health_probe_limiter
        live_state = LiveOperationalState(
            getattr(app.state, "db_pool", None),
            quota_policy=QuotaPolicy(
                soft_threshold=app_settings.quota_soft_threshold,
                hard_threshold=app_settings.quota_hard_threshold,
            ),
        )
        app.state.live_state = live_state
        if getattr(app.state, "db_pool", None) is not None:
            await live_state.refresh()
            resources.task(
                _refresh_live_state(app_settings.live_state_refresh_seconds, live_state)
            )
        if (
            app_settings.credential_usage_poll_enabled
            and app_settings.service_role in {"combined", "maintenance"}
            and getattr(app.state, "db_pool", None) is not None
        ):
            resources.task(
                usage_poll_loop(
                    app_settings.credential_usage_poll_interval_seconds,
                    lambda: getattr(app.state, "db_pool", None),
                    app_settings.credential_encryption_key,
                )
            )
        if (
            app_settings.alert_monitor_enabled
            and app_settings.service_role in {"combined", "maintenance"}
            and getattr(app.state, "db_pool", None) is not None
        ):
            resources.task(
                alert_monitor_loop(
                    app_settings.alert_monitor_interval_seconds,
                    lambda: getattr(app.state, "db_pool", None),
                )
            )
        if (app_settings.health_probe_enabled
                and app_settings.service_role in {"combined", "maintenance"}):
            resources.task(
                health_probe_loop(
                    app_settings.health_probe_interval_seconds,
                    lambda: getattr(app.state, "runtime", None),
                    lambda: getattr(app.state, "health_recorder", None),
                    lambda: getattr(app.state, "health_probe_limiter", None),
                )
            )
        if app.state.admin_verifier is None and app_settings.supabase_url:
            app.state.admin_verifier = SupabaseJWTVerifier(
                app_settings.supabase_url,
                audience=app_settings.supabase_jwt_audience,
                admin_role=app_settings.admin_role,
            )
        if app_settings.workflows_enabled and app_settings.service_role == "combined":
            from gateway.worker_service import workflow_loop

            resources.task(workflow_loop(app))
        try:
            yield
        finally:
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
    app.state.dependency_health = DependencyHealth()
    app.state.admin_verifier = admin_verifier
    app.state.health_recorder = None
    app.state.live_state = LiveOperationalState(
        quota_policy=QuotaPolicy(
            soft_threshold=app_settings.quota_soft_threshold,
            hard_threshold=app_settings.quota_hard_threshold,
        )
    )
    app.state.ready = False
    app.state.drain = DrainState()

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for error in exc.errors():
            detail = {key: value for key, value in error.items() if key != "input"}
            if "ctx" in detail:
                detail["ctx"] = {
                    key: str(value) for key, value in detail["ctx"].items()
                }
            details.append(detail)
        return JSONResponse({"detail": details}, status_code=422)

    @app.middleware("http")
    async def control_plane_mutation_transaction(request: Request, call_next):
        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        is_control_plane = request.url.path.startswith("/api/admin/v1/")
        is_snapshot_operation = "/config/" in request.url.path
        is_manual_probe = request.url.path.endswith("/health/probe")
        is_drain = (request.url.path in {"/api/admin/v1/drain", "/api/admin/v1/capacity"}
                    or request.url.path.startswith("/api/admin/v1/accounting/"))
        pool = getattr(request.app.state, "db_pool", None)
        transactional = (
            is_mutation
            and is_control_plane
            and not is_snapshot_operation
            and not is_manual_probe
            and not is_drain
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
    # Keep admission outside BaseHTTPMiddleware so a slot includes the entire
    # response/background lifetime. Request context also covers early rejections.
    app.add_middleware(InferenceAdmissionMiddleware, settings=app_settings, drain=app.state.drain)
    app.add_middleware(RequestContextMiddleware, settings=app_settings)
    app.include_router(messages_router)
    app.include_router(models_router)
    app.include_router(openai_router)
    app.include_router(workflows_router)
    app.include_router(artifacts_router)
    app.include_router(admin_router)
    app.include_router(reconcile_router)
    app.include_router(control_plane_router)
    app.include_router(operations_router)
    app.include_router(metrics_router)
    app.include_router(accounting_router)

    @app.get("/health", include_in_schema=False)
    @app.head("/health", include_in_schema=False)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/ready", include_in_schema=False)
    @app.head("/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        if not request.app.state.ready or request.app.state.drain.draining:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        if requires_database(request.app.state.settings):
            pool = request.app.state.db_pool
            if not await request.app.state.dependency_health.healthy(pool):
                return JSONResponse({"status": "not_ready"}, status_code=503)
            # Drain or shutdown may have started while the shared probe was pending.
            if (not request.app.state.ready or request.app.state.drain.draining
                    or pool is not request.app.state.db_pool):
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
