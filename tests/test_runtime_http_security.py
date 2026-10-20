from __future__ import annotations

import http.client
import io
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone

import anyio
import pytest
import requests
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
def tls_endpoint(tmp_path):
    """Trust a local certificate for the different IDNA-2003 spelling only."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fass.example")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fass.example")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "certificate.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    client_context = ssl.create_default_context(cafile=str(certificate_path))
    errors: list[Exception] = []
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(5)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    with server_context.wrap_socket(connection, server_side=True) as stream:
                        stream.sendall(b"ok")
            except ssl.SSLError:
                # Expected when the client rejects the trusted certificate's name.
                pass
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], client_context
        finally:
            thread.join(timeout=6)
            assert not thread.is_alive()
            assert not errors


@pytest.mark.parametrize("hostname", ["fass.example", "faß.example"])
async def test_tls_verification_keeps_distinct_idna_hostnames(tls_endpoint, hostname):
    port, context = tls_endpoint

    async def connect():
        return await anyio.connect_tcp(
            "127.0.0.1", port, ssl_context=context, tls=True,
            tls_hostname=hostname, tls_standard_compatible=False,
        )

    if hostname == "faß.example":
        with pytest.raises(ssl.SSLCertVerificationError):
            await connect()
    else:
        async with await connect() as stream:
            assert await stream.receive(2) == b"ok"


def test_requests_stream_rejects_oversized_chunk_header_without_unbounded_read():
    class BoundedReads(io.BytesIO):
        def readline(self, size=-1):
            assert 0 < size <= 65_537, "chunk header read must have a fixed bound"
            return super().readline(size)

    wire = BoundedReads(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"f" * 70_000 + b"\r\n"
    )

    class BufferedSocket:
        def makefile(self, _mode):
            return wire

    original = http.client.HTTPResponse(BufferedSocket())
    original.begin()
    with requests.Response() as response:
        response.raw = urllib3.response.HTTPResponse(
            body=original, headers=original.getheaders(),
            original_response=original, preload_content=False,
        )
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            list(response.iter_content(chunk_size=128))
