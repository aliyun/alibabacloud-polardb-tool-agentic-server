"""Real credential client + PolarDB SDK against synthetic loopback HTTP responses."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from server.aliyun.credential_provider import build_credential_provider
from server.aliyun.polardb_client import OpenAPIError
from server.aliyun.polardb_client_impl import AliyunPolarDBClient
from server.config import AliyunConfig


@contextmanager
def openapi_server(payload, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            requests.append({"query": parse_qs(urlsplit(self.path).query), "body": parse_qs(body),
                             "headers": {key.lower(): value for key, value in self.headers.items()}})
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def local_client(monkeypatch, endpoint):
    monkeypatch.setattr("server.aliyun.polardb_client_impl.resolve_openapi_endpoint", lambda *_args: endpoint)
    provider = build_credential_provider(AliyunConfig(
        credential_mode="direct_ak", region_id="cn-hangzhou", openapi_network="public",
        direct_ak={"access_key_id": "synthetic-wire-ak", "access_key_secret": "synthetic-wire-secret"},
    ))
    client = AliyunPolarDBClient(provider)
    sdk = await client._get_sdk()
    assert sdk._endpoint == endpoint
    sdk._protocol = "http"
    return client


@pytest.mark.parametrize("clusters", [[], [{"DBClusterId": "pc-synthetic", "DBClusterStatus": "Running"}]])
async def test_real_sdk_signs_and_decodes_loopback_cluster_discovery(monkeypatch, clusters):
    with openapi_server({"RequestId": "wire-request-200", "Items": {"DBCluster": clusters}}) as (endpoint, calls):
        client = await local_client(monkeypatch, endpoint)
        result = await client.discover_clusters("cn-hangzhou")
        assert result == [{"cluster_id": item["DBClusterId"], "status": item["DBClusterStatus"]} for item in clusters]
        assert client.last_request_id == "wire-request-200"
        assert len(calls) == 1
        request = calls[0]
        params = {**request["query"], **request["body"]}
        assert params["RegionId"] == ["cn-hangzhou"]
        assert request["headers"].get("x-acs-action", params.get("Action", [None])[0]) == "DescribeDBClusters"
        assert "synthetic-wire-ak" in request["headers"].get("authorization", "") or params.get("AccessKeyId") == ["synthetic-wire-ak"]


async def test_real_sdk_cloud_error_retains_request_id_without_leaking_secret(monkeypatch):
    with openapi_server({"Code": "Forbidden.RAM", "Message": "access_key_secret=must-not-leak",
                         "RequestId": "wire-request-403"}, 403) as (endpoint, calls):
        client = await local_client(monkeypatch, endpoint)
        with pytest.raises(OpenAPIError) as captured:
            await client.discover_clusters("cn-hangzhou")
        assert captured.value.code == "Forbidden.RAM"
        assert captured.value.request_id == "wire-request-403"
        assert "must-not-leak" not in str(captured.value)
        assert len(calls) == 1
