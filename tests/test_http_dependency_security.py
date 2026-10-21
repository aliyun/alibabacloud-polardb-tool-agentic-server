"""Bounded regressions for the runtime HTTP client's trust and cookie boundary."""
from __future__ import annotations

import gc
import hashlib
import platform
import socket
import ssl
import sys
from http.cookies import SimpleCookie

import aiohttp
from aiohttp import web
import httpx
from multidict import CIMultiDict, MultiDict
import pytest
import requests
from yarl import URL


# Public certificate SHA256 fingerprints, not credentials.
COMPROMISED_ROOT = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
ISRG_ROOT_X1 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"
UNAFFECTED_EMSIGN_G1 = "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367"


@pytest.mark.parametrize("client", ["requests", "httpx"])
def test_default_client_trust_excludes_compromised_emsign_root(client):
    if client == "requests":
        context = ssl.create_default_context(cafile=requests.certs.where())
    else:
        context = httpx.create_ssl_context(trust_env=False)
    roots = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in context.get_ca_certs(binary_form=True)
    }

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert ISRG_ROOT_X1 in roots
    assert UNAFFECTED_EMSIGN_G1 in roots
    assert COMPROMISED_ROOT not in roots


async def test_shared_secure_cookie_is_sent_only_over_secure_transport():
    jar = aiohttp.CookieJar()
    jar.update_cookies(SimpleCookie("shared=value; Secure"))

    assert jar.filter_cookies(URL("https://service.example/"))["shared"].value == "value"
    assert "shared" not in jar.filter_cookies(URL("http://service.example/"))


async def test_subdomain_cookie_deletion_preserves_host_only_cookie_scope():
    jar = aiohttp.CookieJar()
    jar.update_cookies({"session": "original"}, URL("https://service.example/"))
    jar.update_cookies(
        SimpleCookie("session=deleted; Domain=service.example; Path=/other; Max-Age=0"),
        URL("https://untrusted.service.example/other"),
    )

    assert jar.filter_cookies(URL("https://service.example/"))["session"].value == "original"
    assert "session" not in jar.filter_cookies(URL("https://sibling.service.example/"))


async def test_large_cookie_max_age_keeps_client_connection_pool_usable():
    async def respond(_request):
        return web.Response(text="ok", headers={"Set-Cookie": "session=value; Max-Age=" + "9" * 500})

    application = web.Application()
    application.router.add_get("/", respond)
    runner = web.AppRunner(application)
    await runner.setup()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/"
        try:
            await web.SockSite(runner, listener).start()
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=1),
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=2),
            ) as client:
                for _ in range(2):
                    async with client.get(endpoint) as response:
                        assert response.status == 200
                        assert await response.text() == "ok"
        finally:
            await runner.cleanup()


@pytest.mark.skipif(platform.python_implementation() != "CPython", reason="CPython reference-count regression")
@pytest.mark.parametrize("mapping_type", [MultiDict, CIMultiDict])
@pytest.mark.parametrize("operation", ["reflected_union", "subtraction"])
def test_multidict_items_operations_release_operand_references(mapping_type, operation):
    mapping = mapping_type({"seed": "value"})
    value = object()
    operand = [(f"header-{index}", value) for index in range(128)]
    before = sys.getrefcount(value)

    if operation == "reflected_union":
        result = operand | mapping.items()
    else:
        result = mapping.items() - operand
    del result
    gc.collect()

    assert sys.getrefcount(value) == before
