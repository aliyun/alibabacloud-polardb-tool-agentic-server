import uvicorn

from server.config import get_config


def main():
    config = get_config()
    uvicorn.run(
        "server.app:create_app",
        factory=True,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )


if __name__ == "__main__":
    main()
