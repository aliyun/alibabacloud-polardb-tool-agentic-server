import asyncio

from server.serve import serve


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
