import uvicorn

from gateway.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "gateway.app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
