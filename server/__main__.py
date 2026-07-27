import uvicorn


def main():
    uvicorn.run(
        "server.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=18760,
        log_level="info",
    )


if __name__ == "__main__":
    main()
