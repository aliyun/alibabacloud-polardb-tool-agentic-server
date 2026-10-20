"""Rehearse stopped 1.0.7 -> 1.0.8 upgrade on a new private local MySQL server."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.dev.maintenance_proxy import MaintenanceProxy  # noqa: E402
from scripts.dev.maintenance_safety import (  # noqa: E402
    ACCEPTANCE_GATES, owned_name, require_owned_datadir, require_stopped, seal_recovery_set, verify_recovery_set,
)
from scripts.dev.run_local_regression import clean_environment, free_port  # noqa: E402

SOURCE_HEAD = "2c3d4e5f6a7b"
TARGET_HEAD = "c7d8e9f0a1b2"


def digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


async def query_async(url: str, statement: str, parameters: dict) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), parameters)
            return [dict(row) for row in result.mappings()] if result.returns_rows else []
    finally:
        await engine.dispose()


def query(url: str, statement: str, **parameters) -> list[dict]:
    return asyncio.run(query_async(url, statement, parameters))


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


class Rehearsal:
    def __init__(self, args, temporary: Path):
        self.args, self.temporary = args, temporary
        self.output = args.output_dir.resolve()
        self.env = clean_environment(dict(os.environ))
        for name in ("MYSQL_PWD", "MYSQL_TEST_LOGIN_FILE"):
            self.env.pop(name, None)
        self.env.update(NO_PROXY="127.0.0.1,localhost", PYTHONUNBUFFERED="1")
        self.source = temporary / "source107"
        self.target_revision = self.git("rev-parse", "HEAD")
        self.source_revision = self.git("rev-parse", f"{args.source_ref}^{{commit}}")
        self.port = free_port()
        self.processes: list[subprocess.Popen] = []
        self.clients: list[httpx.Client] = []
        self.databases: list[str] = []
        self.users: list[str] = []
        self.handles = []
        self.mysql_process = None
        self.proxy = None
        self.evidence = {"source_revision": self.source_revision, "target_revision": self.target_revision,
            "production_access": False, "provider_changed": False, "phases": [], "cleanup_complete": False}
        self.operation = "maintenance-108-" + secrets.token_hex(8)
        self.state = dict.fromkeys(ACCEPTANCE_GATES, False)

    def git(self, *args) -> str:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    def run(self, name: str, command: list, *, env=None, cwd=None, expected: str | None = None,
            input_data: bytes | None = None, timeout=180):
        started = time.monotonic()
        result = subprocess.run([str(arg) for arg in command], env=env or self.env, cwd=cwd or ROOT,
            input=input_data, capture_output=True, timeout=timeout)
        (self.output / f"{name}.log").write_bytes(result.stdout + result.stderr)
        self.evidence.setdefault("commands", []).append({"name": name, "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 3), "expected_error": expected})
        if expected is not None:
            assert result.returncode != 0 and expected.encode() in result.stdout + result.stderr, name
        elif result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); inspect its log")
        return result

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        entry = {"name": name, "passed": False}
        self.evidence["phases"].append(entry)
        print("Running " + name, flush=True)
        try:
            yield entry
            entry["passed"] = True
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def save(self):
        (self.output / "rehearsal.json").write_text(json.dumps(self.evidence, indent=2, default=str) + "\n")

    def setup(self):
        manifest = json.loads((ROOT / "server/db/schema_manifest.json").read_text())
        assert manifest["head"] == TARGET_HEAD and manifest["release_classification"] == "CONTRACT"
        archive = subprocess.check_output(["git", "archive", self.source_revision], cwd=ROOT)
        self.source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            contents.extractall(self.source, filter="data")
        old_manifest = json.loads((self.source / "server/db/schema_manifest.json").read_text())
        assert old_manifest["head"] == getattr(self.args, "source_head", SOURCE_HEAD) and old_manifest["generation"] == 1
        self.evidence.update(source_lock_sha256=digest((self.source / "uv.lock").read_bytes()),
            target_lock_sha256=digest((ROOT / "uv.lock").read_bytes()), target_manifest=manifest | {"tables": "omitted"},
            target_runtime_tree=self.git("rev-parse", "HEAD:server"),
            stable_operation_id=self.operation, packaging="pinned source and target with separate frozen Python environments")
        self.run("source-dependencies", ["uv", "sync", "--frozen", "--no-dev"], cwd=self.source, timeout=300)
        self.pythons = {"107": self.source / ".venv/bin/python", "108": Path(sys.executable)}
        self.sources = {"107": self.source, "108": ROOT}
        if self.args.chrome:
            for name, source in (("107", self.source), ("108", ROOT)):
                self.run(name + "-web-install", ["npm", "ci", "--no-audit", "--no-fund"], cwd=source / "web", timeout=300)
                self.run(name + "-web-build", ["npm", "run", "build"], cwd=source / "web", timeout=180)
        basedir = self.args.mysql_basedir.resolve()
        prefix = ["arch", "-" + self.args.mysql_arch] if self.args.mysql_arch else []
        self.mysqld = [*prefix, basedir / "bin/mysqld"]
        data = self.temporary / "mysql"
        self.run("mysql-initialize", [*self.mysqld, "--no-defaults", f"--basedir={basedir}",
            f"--datadir={data}", "--initialize-insecure"], timeout=120)
        handle = (self.output / "mysql-console.log").open("w")
        self.handles.append(handle)
        self.mysql_process = subprocess.Popen([str(arg) for arg in [*self.mysqld, "--no-defaults",
            f"--basedir={basedir}", f"--datadir={data}", f"--port={self.port}",
            f"--socket={self.temporary / 'mysql.sock'}", "--bind-address=127.0.0.1", "--mysqlx=OFF", "--skip-log-bin",
            "--innodb-buffer-pool-size=67108864", f"--log-error={self.output / 'mysql-error.log'}"]],
            env=self.env, stdout=handle, stderr=subprocess.STDOUT)
        self.admin_url = f"mysql+asyncmy://root@127.0.0.1:{self.port}/mysql"
        deadline = time.monotonic() + 60
        while True:
            if self.mysql_process.poll() is not None:
                raise RuntimeError("Private MySQL stopped; inspect mysql-error.log")
            try:
                server = query(self.admin_url, "SELECT @@version AS version, @@datadir AS datadir")[0]
                version = server["version"]
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise TimeoutError("Private MySQL did not start") from None
                time.sleep(.1)
        require_owned_datadir(server["datadir"], data)
        self.evidence["mysql_data_directory_owned"] = True
        if self.args.expected_mysql_version:
            assert version == self.args.expected_mysql_version, version
        self.evidence["mysql_version"] = version
        self.evidence["mysql_binary"] = str(basedir / "bin/mysqld")
        self.database = self.create_database()
        self.url = f"mysql+asyncmy://root@127.0.0.1:{self.port}/{self.database}"
        self.app_env = dict(self.env, PAS_DATABASE_URL=self.url,
            PAS_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
            PAS_MAINT_PASSWORD="Maintenance!" + secrets.token_hex(16),
            PAS_MAINT_LOG_DIR=str(self.temporary / "app-logs"),
            PAS_IDENTITY_SYNC_STATE_DIR=str(self.temporary / "identity-state"))
        self.proxy = MaintenanceProxy()

    def create_database(self) -> str:
        name = owned_name("pas_maint_" + secrets.token_hex(8))
        query(self.admin_url, f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        self.databases.append(name)
        return name

    def cli(self, version: str, name: str, *args, env=None, expected=None):
        return self.run(name, [self.pythons[version], ROOT / "scripts/dev/maintenance_app.py",
            self.sources[version], *args], env=env or self.app_env, cwd=self.sources[version], expected=expected)

    def head(self) -> str:
        return query(self.url, "SELECT version_num FROM alembic_version")[0]["version_num"]

    def start(self, version: str, name: str, *, expected: str | None = None):
        port = free_port()
        log = self.output / f"{name}.log"
        handle = log.open("w")
        self.handles.append(handle)
        process = subprocess.Popen([str(self.pythons[version]), str(ROOT / "scripts/dev/maintenance_app.py"),
            str(self.sources[version]), "serve", "--local-sso-dev"], cwd=self.sources[version],
            env=dict(self.app_env, PAS_SERVER_PORT=str(port), PAS_STATIC_DIR=str(self.sources[version] / "web/dist")),
            stdout=handle, stderr=subprocess.STDOUT)
        self.processes.append(process)
        process.maintenance_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                handle.flush()
                if expected:
                    assert process.returncode != 0 and expected in log.read_text(), name
                    return process
                raise RuntimeError(name + " stopped before readiness; inspect its log")
            try:
                if httpx.get(process.maintenance_url + "/readyz", timeout=.5, trust_env=False).status_code == 200:
                    assert expected is None, "Incompatible old version unexpectedly started"
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        stop(process)
        raise TimeoutError(name + " did not finish startup")

    def client(self, url: str):
        client = httpx.Client(base_url=url, timeout=15, trust_env=False,
                             headers={"X-PAS-CSRF": "1", "Connection": "close"})
        self.clients.append(client)
        response = client.post("/auth/login", json={"username": "maintenance-admin", "password": self.app_env["PAS_MAINT_PASSWORD"]})
        assert response.status_code == 200, "Administrator login failed"
        for path in ("/auth/me", "/api/users", "/api/agents", "/api/identity-sources"):
            assert client.get(path).status_code == 200, path
        return client

    def create_agent(self, client, name: str) -> str:
        response = client.post("/api/agents", json={"name": name})
        assert response.status_code == 201, "Agent write failed"
        return response.json()["id"]

    def token_hash(self, client) -> str:
        response = client.post(f"/api/agents/{self.source_agent}/token/reveal")
        assert response.status_code == 200, "Preserved token decryption failed"
        return digest(response.json()["token"])

    def no_application_sessions(self):
        deadline = time.monotonic() + 10
        while True:
            sessions = query(self.admin_url, "SELECT COUNT(*) AS count FROM information_schema.PROCESSLIST WHERE DB=:db", db=self.database)[0]["count"]
            if sessions == 0:
                return
            if time.monotonic() > deadline:
                raise RuntimeError("Old database sessions remain after stopping readers/writers")
            time.sleep(.1)

    def closed(self):
        for path in ("/readyz", "/auth/me", "/api/agents"):
            assert httpx.get(self.proxy.url + path, timeout=5, trust_env=False).status_code == 503, path

    def row_snapshot(self, template: dict | None = None) -> dict:
        tables = query(self.url, "SELECT table_name AS name FROM information_schema.tables WHERE table_schema=:db AND table_type='BASE TABLE'", db=self.database)
        result = {}
        for table in tables:
            name = table["name"]
            if name in {"alembic_version", "system_config"} or template is not None and name not in template:
                continue
            columns = template[name]["columns"] if template is not None else [c["name"] for c in query(self.url,
                "SELECT column_name AS name FROM information_schema.columns WHERE table_schema=:db AND table_name=:name ORDER BY ordinal_position",
                db=self.database, name=name)]
            rows = query(self.url, "SELECT " + ",".join("`" + c + "`" for c in columns) + " FROM `" + name + "`")
            values = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
            result[name] = {"columns": columns, "rows": len(rows), "sha256": digest("\n".join(values))}
        return result

    def physical_schema(self, name: str) -> dict:
        tables = {row['name'] for row in query(self.url,
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema=:db", db=self.database)}
        affected = ("agents", "enterprise_identity_sources", "enterprise_principal_assignments",
                    "external_knowledge_scope_snapshots", "external_knowledge_scope_grants", "dingtalk_login_states")
        objects = {}
        for table in affected:
            objects[table] = list(query(self.url, f"SHOW CREATE TABLE `{table}`")[0].values())[1] if table in tables else None
        result = {"server_version": self.evidence["mysql_version"], "head": self.head(), "objects": objects}
        (self.output / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
        return result

    def backup(self):
        self.bundle = self.temporary / "recovery-set"
        self.bundle.mkdir(mode=0o700)
        argv = [shutil.which("mysqldump"), "--no-defaults", "--protocol=TCP", "--host=127.0.0.1",
            f"--port={self.port}", "--user=root", "--skip-password", "--single-transaction", "--quick", "--hex-blob",
            "--routines", "--events", "--triggers", "--no-tablespaces", "--column-statistics=0", "--set-gtid-purged=OFF", self.database]
        result = subprocess.run(argv, env=self.env, capture_output=True, check=True, timeout=120)
        (self.bundle / "metadata.sql").write_bytes(result.stdout)
        (self.bundle / "root-key").write_text(self.app_env["PAS_ENCRYPTION_KEY"])
        (self.bundle / "runtime.json").write_text(json.dumps({"environment": {
            key: self.app_env[key] for key in ("PAS_MAINT_LOG_DIR", "PAS_IDENTITY_SYNC_STATE_DIR")},
            "source_revision": self.source_revision, "target_revision": self.target_revision}))
        for file in self.bundle.iterdir():
            file.chmod(0o600)
        manifest = seal_recovery_set(self.bundle, source=self.source_revision, target=self.target_revision)
        self.evidence["recovery_manifest"] = manifest
        self.evidence["backup_bytes"] = (self.bundle / "metadata.sql").stat().st_size
        self.evidence["source_rows"] = self.row_snapshot()
        probe = self.create_database()
        self.restore(probe)
        self.cli("107", "source-backup-restore-check", "database", "check", "--format", "json",
            env=dict(self.app_env, PAS_DATABASE_URL=f"mysql+asyncmy://root@127.0.0.1:{self.port}/{probe}"))
        self.state["backup_restore_verified"] = True

    def restore(self, name: str):
        owned_name(name)
        require_stopped(self.processes)
        verify_recovery_set(self.bundle, source=self.source_revision, target=self.target_revision)
        query(self.admin_url, f"DROP DATABASE `{name}`")
        query(self.admin_url, f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        subprocess.run([shutil.which("mysql"), "--no-defaults", "--protocol=TCP", "--host=127.0.0.1",
            f"--port={self.port}", "--user=root", "--skip-password", name], env=self.env,
            input=(self.bundle / "metadata.sql").read_bytes(), capture_output=True, check=True, timeout=120)
        self.app_env["PAS_ENCRYPTION_KEY"] = (self.bundle / "root-key").read_text()
        runtime = json.loads((self.bundle / "runtime.json").read_text())["environment"]
        for key in ("PAS_MAINT_LOG_DIR", "PAS_IDENTITY_SYNC_STATE_DIR"):
            self.app_env[key] = runtime[key]

    def restore_to_new_database(self):
        name = self.create_database()
        self.restore(name)
        self.database = name
        self.url = f"mysql+asyncmy://root@127.0.0.1:{self.port}/{name}"
        self.app_env["PAS_DATABASE_URL"] = self.url

    def migrate(self, name: str, *, env=None, expected=None):
        require_stopped(self.processes)
        if not self.state["old_writers_stopped"] or not self.state["backup_restore_verified"]:
            raise RuntimeError("Verified stopped-writer and restored-backup gates are required")
        verify_recovery_set(self.bundle, source=self.source_revision, target=self.target_revision)
        self.closed()
        return self.cli("108", name, "database", "migrate", "--maintenance", "--operation-id", self.operation,
                        env=env, expected=expected)

    def chromium(self, name: str, url: str):
        if self.args.chrome:
            self.run("chrome-" + name, ["node", ROOT / "web/e2e/maintenance-probe.mjs"],
                env=dict(self.app_env, PAS_MAINT_URL=url, PAS_MAINT_SCREENSHOT=str(self.output / (name + ".png"))))

    def target_acceptance(self, name: str):
        self.cli("108", name + "-check", "database", "check", "--format", "json")
        self.state["migration_checked"] = True
        before = self.cli("108", name + "-inspect", "database", "inspect", "--format", "json").stdout
        physical_before = self.physical_schema(name + "-physical-before-replay")
        self.migrate(name + "-replay")
        self.cli("108", name + "-replay-check", "database", "check", "--format", "json")
        after = self.cli("108", name + "-replay-inspect", "database", "inspect", "--format", "json").stdout
        assert json.loads(before) == json.loads(after), "Replay changed inspected schema"
        assert physical_before == self.physical_schema(name + "-physical-after-replay"), "Replay changed physical objects"
        assert self.head() == TARGET_HEAD
        self.state["replay_checked"] = True
        assert self.row_snapshot(self.evidence["source_rows"]) == self.evidence["source_rows"], "Source data changed"
        self.state["physical_data_verified"] = True
        replicas = [self.start("108", name + "-a"), self.start("108", name + "-b")]
        for index, process in enumerate(replicas):
            client = self.client(process.maintenance_url)
            assert self.source_agent in client.get("/api/agents").text
            assert self.token_hash(client) == self.source_token_hash
            self.create_agent(client, f"Maintenance {name} replica-{index} validated write")
        self.state["both_replicas_authenticated"] = True
        self.closed()
        self.proxy.open([p.maintenance_url for p in replicas], self.state)
        self.chromium(name, replicas[0].maintenance_url)
        return replicas

    def exercise(self):
        with self.phase("discovery-and-source-baseline"):
            self.cli("107", "source-migrate", "database", "migrate")
            self.cli("107", "source-initialize", "initialize-rehearsal")
            self.cli("107", "source-check", "database", "check", "--format", "json")
            self.cli("108", "target-read-only-discovery", "database", "inspect", "--format", "json")
            assert self.head() == SOURCE_HEAD
            self.physical_schema("source-physical-discovery")
            replicas = [self.start("107", "source-a"), self.start("107", "source-b")]
            clients = [self.client(p.maintenance_url) for p in replicas]
            self.source_agent = self.create_agent(clients[0], "Maintenance source retained Agent")
            self.create_agent(clients[1], "Maintenance peer retained Agent")
            self.source_token_hash = self.token_hash(clients[0])
            response = clients[0].post("/api/identity-sources", json={"name": "Maintenance Feishu fixture",
                "provider": "feishu", "app_id": "synthetic",
                "app_secret": "synthetic-maintenance-only", "bind_all_spaces": False})
            assert response.status_code == 201, f"Source identity fixture failed: HTTP {response.status_code}"
            self.proxy.start_source([p.maintenance_url for p in replicas])
            self.client(self.proxy.url)
            self.chromium("source107", replicas[0].maintenance_url)
        with self.phase("drain-stop-backup-and-restore-probe"):
            self.proxy.drain()
            self.closed()
            for process in replicas:
                stop(process)
            require_stopped(self.processes)
            self.no_application_sessions()
            self.state["old_writers_stopped"] = True
            self.backup()
        with self.phase("pre-ddl-failure-and-writer-recovery"):
            query(self.admin_url, "SET GLOBAL read_only=ON")
            try:
                self.migrate("read-only-refused", expected="DATABASE_MIGRATION_READ_ONLY")
                assert self.head() == SOURCE_HEAD
                assert self.row_snapshot() == self.evidence["source_rows"]
            finally:
                query(self.admin_url, "SET GLOBAL read_only=OFF")
            self.migrate("writer-repaired-upgrade")
        with self.phase("wrong-key-forward-recovery"):
            self.cli("108", "wrong-key-refused", "database", "check", expected="DATABASE_ENCRYPTION_KEY_MISMATCH",
                env=dict(self.app_env, PAS_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode()))
            self.cli("108", "matching-key-restored", "database", "check", "--format", "json")
        with self.phase("two-target-replicas-and-reopen"):
            replicas = self.target_acceptance("target108")
            client = self.client(self.proxy.url)
            self.post_backup_agent = self.create_agent(client, "Maintenance post-backup write")
            response = client.post("/api/identity-sources", json={"name": "Maintenance DingTalk fixture",
                "provider": "dingtalk", "tenant_id": "maintenance-local", "client_id": "synthetic",
                "client_secret": "synthetic-maintenance-only", "bind_all_spaces": False})
            assert response.status_code == 201
        with self.phase("image-only-refused-and-paired-rollback"):
            self.proxy.drain()
            self.closed()
            for process in replicas:
                stop(process)
            require_stopped(self.processes)
            self.start("107", "image-only-rollback-refused", expected="DATABASE_SCHEMA_CONTRACT_INVALID")
            # Prove restoration applies the original runtime pair, rather than
            # accidentally succeeding with values still present in memory.
            self.app_env["PAS_ENCRYPTION_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
            self.app_env["PAS_IDENTITY_SYNC_STATE_DIR"] = str(self.temporary / "wrong-state-dir")
            self.restore_to_new_database()
            assert self.head() == SOURCE_HEAD
            assert self.row_snapshot() == self.evidence["source_rows"]
            self.cli("107", "rollback-source-schema-check", "database", "check", "--format", "json")
            self.physical_schema("rollback-source-physical")
            replicas = [self.start("107", "rollback107-a"), self.start("107", "rollback107-b")]
            for index, process in enumerate(replicas):
                client = self.client(process.maintenance_url)
                assert self.source_agent in client.get("/api/agents").text
                assert self.post_backup_agent not in client.get("/api/agents").text
                assert self.token_hash(client) == self.source_token_hash
                self.create_agent(client, f"Maintenance rollback replica-{index} validated write")
            rollback_gates = {"target_writers_stopped": True, "source_schema_checked": True,
                "paired_backup_restored": True, "both_replicas_authenticated": True}
            self.proxy.open_rollback([p.maintenance_url for p in replicas], rollback_gates)
            self.client(self.proxy.url)
            self.chromium("rollback107", replicas[0].maintenance_url)
            self.evidence["rollback"] = {"source_data_preserved": True, "both_replicas_authenticated": True,
                "original_token_decrypted": True, "post_backup_write_lost_as_expected": True,
                "paired_runtime_restored": self.app_env["PAS_IDENTITY_SYNC_STATE_DIR"] == str(self.temporary / "identity-state")}
            assert self.evidence["rollback"]["paired_runtime_restored"]
            self.proxy.drain()
            for process in replicas:
                stop(process)
        with self.phase("partial-ddl-failure-refuses-blind-retry"):
            self.restore_to_new_database()
            user = owned_name("pas_maint_" + secrets.token_hex(8))
            self.users.append(user)
            # Only this private server is reachable; the synthetic account has no password.
            query(self.admin_url, f"CREATE USER '{user}'@'127.0.0.1' IDENTIFIED BY ''")
            query(self.admin_url, f"GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,INDEX,REFERENCES ON `{self.database}`.* TO '{user}'@'127.0.0.1'")
            for table in ("agents", "enterprise_principal_assignments"):
                query(self.admin_url, f"GRANT ALTER ON `{self.database}`.`{table}` TO '{user}'@'127.0.0.1'")
            limited_env = dict(self.app_env, PAS_DATABASE_URL=f"mysql+asyncmy://{user}@127.0.0.1:{self.port}/{self.database}")
            self.migrate("partial-ddl-refused", env=limited_env, expected="DATABASE_MIGRATION_FAILED")
            assert self.head() == "b3c4d5e6f7a8", self.head()
            columns = query(self.url, "SHOW COLUMNS FROM agents LIKE 'external_knowledge_auto_bind_enabled'")
            assert columns, "The intended committed DDL was not observed"
            records = query(self.url, "SELECT config_value FROM system_config WHERE config_key LIKE 'schema.migration.%'")
            assert any(json.loads(row['config_value']).get('status') == 'FAILED' for row in records)
            self.evidence["partial_ddl"] = {"head": self.head(), "added_agents_column_present": True, "failed_checkpoint_present": True}
            self.physical_schema("partial-ddl-physical-evidence")
            self.cli("108", "partial-physical-inspect", "database", "inspect", "--format", "json")
            self.migrate("partial-root-retry-refused", expected="DATABASE_MIGRATION_REPAIR_REQUIRED")
            self.closed()
        with self.phase("partial-ddl-paired-restore-and-reupgrade"):
            self.restore_to_new_database()
            assert self.head() == SOURCE_HEAD and self.row_snapshot() == self.evidence["source_rows"]
            self.state.update(migration_checked=False, replay_checked=False, physical_data_verified=False,
                              both_replicas_authenticated=False)
            self.migrate("restored-source-reupgrade")
            replicas = self.target_acceptance("recovered108")
            client = self.client(self.proxy.url)
            self.create_agent(client, "Maintenance recovered write")
            self.evidence["partial_ddl"]["paired_restore_and_reupgrade"] = True
            self.evidence["acceptance_gates"] = self.state
            for process in replicas:
                stop(process)

    def close(self):
        if self.proxy:
            self.proxy.close()
        for client in self.clients:
            client.close()
        for process in self.processes:
            stop(process)
        if self.mysql_process and self.mysql_process.poll() is None:
            try:
                for name in self.databases:
                    owned_name(name)
                    query(self.admin_url, f"DROP DATABASE IF EXISTS `{name}`")
                for name in self.users:
                    owned_name(name)
                    query(self.admin_url, f"DROP USER IF EXISTS '{name}'@'127.0.0.1'")
            finally:
                stop(self.mysql_process)
        for handle in self.handles:
            handle.close()
        self.evidence["cleanup_complete"] = all(p.poll() is not None for p in self.processes) and (
            self.mysql_process is None or self.mysql_process.poll() is not None)
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mysql-basedir", required=True, type=Path, help="Local binary directory; never an existing server")
    parser.add_argument("--mysql-arch", choices=("x86_64", "arm64"))
    parser.add_argument("--expected-mysql-version")
    parser.add_argument("--source-ref", required=True, help="Locally available immutable source revision with the required schema head")
    parser.add_argument("--chrome", action="store_true", help="Also build both UIs and smoke with actual Google Chrome")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / "rehearsal.json").exists():
        parser.error("use a new output directory; preserve prior failure evidence")
    with tempfile.TemporaryDirectory(prefix="pas-maint-", dir="/tmp") as temporary:
        runner = Rehearsal(args, Path(temporary))
        try:
            with runner.phase("private-environment-setup"):
                runner.setup()
            runner.exercise()
            runner.evidence["passed"] = True
        finally:
            runner.close()
    runner.evidence["runtime_directory_removed"] = not Path(temporary).exists()
    runner.save()
    print("Maintenance rehearsal passed; private runtime and paired recovery secrets removed", flush=True)


if __name__ == "__main__":
    main()
