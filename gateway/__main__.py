import asyncio

import uvicorn

from gateway.config import get_settings


def main() -> None:
    settings = get_settings()
    if settings.service_role in {"workflow", "maintenance"}:
        from gateway.worker_service import serve_role

        asyncio.run(serve_role(settings))
        return
    uvicorn.run(
        "gateway.app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    main()
