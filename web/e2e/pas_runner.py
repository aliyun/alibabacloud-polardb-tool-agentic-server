"""Run the real PAS CLI with loopback-only networking for browser tests.

No API, SDK, credential provider, or database response is replaced. Denying
outbound DNS/TCP makes connection failures deterministic without cloud access.
This is a test guard, not an OS security sandbox.
"""

import ipaddress
import runpy
import sys
from pathlib import Path


def require_loopback(host: object) -> None:
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host)).is_loopback:
            return
    except ValueError:
        pass
    raise ConnectionRefusedError("Browser E2E denies non-loopback networking")


def network_guard(event: str, args: tuple) -> None:
    if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        require_loopback(args[0])
    elif event in {"socket.connect", "socket.sendto"}:
        address = args[1] if event == "socket.connect" else args[-1]
        if isinstance(address, tuple):
            require_loopback(address[0])


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.addaudithook(network_guard)
    if sys.argv[1:] == ["seed-layout"]:
        runpy.run_path(str(Path(__file__).with_name("seed_layout.py")), run_name="__main__")
    else:
        runpy.run_module("server.cli", run_name="__main__")
