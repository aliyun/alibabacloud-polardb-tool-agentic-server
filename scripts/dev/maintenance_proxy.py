"""A disposable loopback maintenance front door; this is not a Provider emulator."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx

from scripts.dev.maintenance_safety import ROLLBACK_GATES, require_acceptance


class MaintenanceProxy:
    def __init__(self):
        self.targets: tuple[str, ...] = ()
        self.requests = 0
        self.maintenance_started = False
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                targets = proxy.targets
                if not targets:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"status":"maintenance"}')
                    return
                target = targets[proxy.requests % len(targets)]
                proxy.requests += 1
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                response = httpx.request(self.command, target + self.path, content=body,
                    headers={key: value for key, value in self.headers.items()
                             if key.lower() not in {"host", "connection", "content-length"}},
                    timeout=15, trust_env=False)
                self.send_response(response.status_code)
                for key, value in response.headers.multi_items():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def open(self, targets: list[str], evidence: dict):
        require_acceptance(evidence)
        self._set_targets(targets)

    def open_rollback(self, targets: list[str], evidence: dict):
        require_acceptance(evidence, ROLLBACK_GATES)
        self._set_targets(targets)

    def _set_targets(self, targets: list[str]):
        if len(targets) != 2 or any(not target.startswith("http://127.0.0.1:") for target in targets):
            raise ValueError("Two owned loopback replicas are required")
        self.targets = tuple(targets)

    def start_source(self, targets: list[str]):
        if self.maintenance_started or self.targets:
            raise RuntimeError("Cannot restart source traffic after maintenance begins")
        self._set_targets(targets)

    def drain(self):
        self.maintenance_started = True
        self.targets = ()

    def close(self):
        self.drain()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
