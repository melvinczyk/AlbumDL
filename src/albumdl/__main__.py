import uvicorn

from .server.app import Settings, create_app


def main() -> None:
    settings = Settings.from_env()
    print(f"albumdl on http://{settings.host}:{settings.port}", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
