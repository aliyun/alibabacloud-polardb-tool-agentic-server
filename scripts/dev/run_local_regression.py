"""Run acceptance on private native MySQL/PostgreSQL servers; never use an existing DB."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def owned_database_name(name: str) -> str:
    if not re.fullmatch(r"pas_local_reg_[0-9a-f]{32}", name):
        raise ValueError("Only an owned random regression database may be created/dropped")
    return name


def clean_environment(values: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in values.items() if not key.startswith(("PAS_", "ALIBABA_CLOUD_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "pythonpath"}}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def integration_environment(candidate: str | None, mysql: str | None, kind: str | None) -> dict[str, str]:
    if mysql and not candidate:
        raise ValueError("Kubernetes acceptance requires --candidate-image")
    if kind and not mysql:
        raise ValueError("--kind-image requires --kubernetes-mysql-image")
    env = {}
    if candidate:
        env.update(PAS_RUN_ENV_GENERATOR_MYSQL_TEST="1", PAS_TEST_IMAGE=candidate)
    if mysql:
        env.update(PAS_RUN_KUBERNETES_INTEGRATION="1", PAS_TEST_MYSQL_IMAGE=mysql)
    if kind:
        env["PAS_TEST_KIND_IMAGE"] = kind
    return env


class LocalDatabases:
    def __init__(self, directory: Path, output: Path, env: dict[str, str]):
        self.directory, self.output, self.env = directory, output, env
        self.mysql = shutil.which("mysql")
        self.mysqld = shutil.which("mysqld")
        self.pg_ctl = shutil.which("pg_ctl")
        self.initdb = shutil.which("initdb")
        self.psql = shutil.which("psql")
        if not all((self.mysql, self.mysqld, self.pg_ctl, self.initdb, self.psql)):
            raise RuntimeError("Install native MySQL server/client and PostgreSQL (mysqld/mysql/initdb/pg_ctl/psql)")
        self.mysql_port, self.pg_port = free_port(), free_port()
        self.mysql_process: subprocess.Popen | None = None
        self.pg_started = False

    def sql(self, dialect: str, query: str) -> str:
        if dialect == "mysql":
            argv = [self.mysql, "--no-defaults", "--protocol=tcp", "--host=127.0.0.1",
                    f"--port={self.mysql_port}", "--user=root", "--batch", "--skip-column-names", "--execute", query]
        else:
            argv = [self.psql, "-X", "-h", "127.0.0.1", "-p", str(self.pg_port), "-U", "pas_test",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-At", "-c", query]
        return subprocess.run(argv, env=self.env, check=True, capture_output=True, text=True, timeout=20).stdout.strip()

    def start(self) -> None:
        mysql_data = self.directory / "mysql"
        mysql_data.mkdir()
        base = str(Path(self.mysqld).resolve().parents[1])
        log = self.output / "native-mysql.log"
        subprocess.run([self.mysqld, "--no-defaults", f"--basedir={base}", f"--datadir={mysql_data}",
                        "--initialize-insecure", f"--log-error={log}"], env=self.env, check=True, timeout=90)
        self.mysql_process = subprocess.Popen([
            self.mysqld, "--no-defaults", f"--basedir={base}", f"--datadir={mysql_data}",
            f"--socket={self.directory / 'mysql.sock'}", f"--port={self.mysql_port}",
            "--bind-address=127.0.0.1", "--mysqlx=OFF", "--skip-log-bin",
            "--innodb-buffer-pool-size=67108864", f"--pid-file={self.directory / 'mysql.pid'}",
            f"--log-error={log}",
        ], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while True:
            if self.mysql_process.poll() is not None:
                raise RuntimeError(f"Private MySQL exited; inspect {log}")
            try:
                self.mysql_version = self.sql("mysql", "SELECT @@version")
                break
            except subprocess.SubprocessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Private MySQL readiness timed out; inspect {log}") from None
                time.sleep(.1)
        pg_data = self.directory / "postgres"
        with (self.output / "native-postgres-init.log").open("w") as output:
            subprocess.run([self.initdb, "-D", str(pg_data), "-U", "pas_test", "-A", "trust", "--no-locale", "-E", "UTF8"],
                           env=self.env, check=True, stdout=output, stderr=subprocess.STDOUT, timeout=60)
        subprocess.run([self.pg_ctl, "-D", str(pg_data), "-l", str(self.output / "native-postgres.log"),
                        "-o", f"-h 127.0.0.1 -p {self.pg_port} -k {self.directory}", "-w", "start"],
                       env=self.env, check=True, stdout=subprocess.DEVNULL, timeout=60)
        self.pg_started = True
        self.pg_version = self.sql("postgresql", "SHOW server_version")

    @contextmanager
    def database(self, dialect: str):
        name = owned_database_name("pas_local_reg_" + secrets.token_hex(16))
        if dialect == "mysql":
            self.sql(dialect, f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
            url = f"mysql+asyncmy://root@127.0.0.1:{self.mysql_port}/{name}"
        else:
            self.sql(dialect, f'CREATE DATABASE "{name}"')
            url = f"postgresql+asyncpg://pas_test@127.0.0.1:{self.pg_port}/{name}"
        try:
            yield url
        finally:
            owned_database_name(name)
            quote = "`" if dialect == "mysql" else '"'
            self.sql(dialect, f"DROP DATABASE {quote}{name}{quote}")

    def close(self) -> None:
        try:
            if self.pg_started:
                subprocess.run([self.pg_ctl, "-D", str(self.directory / "postgres"), "-m", "fast", "-w", "stop"],
                               env=self.env, check=True, stdout=subprocess.DEVNULL, timeout=30)
        finally:
            if self.mysql_process is not None and self.mysql_process.poll() is None:
                self.mysql_process.terminate()
                try:
                    self.mysql_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.mysql_process.kill()
                    self.mysql_process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--full", action="store_true", help="Also run the complete backend suite on fresh private databases")
    parser.add_argument("--candidate-image", help="Explicit local candidate image for real container acceptance")
    parser.add_argument("--kubernetes-mysql-image", help="Also run kind/Helm acceptance with this MySQL image")
    parser.add_argument("--kind-image", help="Optional kind node image for Kubernetes acceptance")
    args = parser.parse_args()
    try:
        integration = integration_environment(args.candidate_image, args.kubernetes_mysql_image, args.kind_image)
    except ValueError as error:
        parser.error(str(error))
    if integration and not args.full:
        parser.error("container acceptance requires --full")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = clean_environment(dict(os.environ))
    # This explicit test-only parser may be a standalone Compose executable.
    if os.environ.get("PAS_TEST_COMPOSE_BINARY"):
        env["PAS_TEST_COMPOSE_BINARY"] = os.environ["PAS_TEST_COMPOSE_BINARY"]
    env.update(PAS_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode(), NO_PROXY="127.0.0.1,localhost")
    results: dict = {"runs": [], "production_access": False, "cleanup_complete": False}

    def run(name: str, argv: list[str], run_env: dict[str, str]) -> None:
        print(f"Running {name}", flush=True)
        log = output / f"{name}.log"
        with log.open("w") as stream:
            result = subprocess.run(argv, cwd=ROOT, env=run_env, stdout=stream, stderr=subprocess.STDOUT)
        results["runs"].append({"name": name, "exit_code": result.returncode, "log": log.name})
        if result.returncode:
            raise RuntimeError(f"{name} failed; inspect {log}")

    with tempfile.TemporaryDirectory(prefix="pas-local-reg-", dir="/tmp") as temporary:
        databases = LocalDatabases(Path(temporary), output, env)
        try:
            databases.start()
            results.update(mysql_version=databases.mysql_version, postgresql_version=databases.pg_version)
            for dialect in ("sqlite", "mysql", "postgresql"):
                with ExitStack() as stack:
                    run_env = dict(env, PAS_TEST_SCALABILITY_DATABASE_OK="1")
                    if dialect != "sqlite":
                        run_env["PAS_DATABASE_URL"] = stack.enter_context(databases.database(dialect))
                    run(f"matrix-{dialect}", ["sh", "scripts/ci/test-migrations.sh", dialect], run_env)
            if args.full:
                with ExitStack() as stack:
                    mysql = stack.enter_context(databases.database("mysql"))
                    postgres = stack.enter_context(databases.database("postgresql"))
                    for dialect, url in (("mysql", mysql), ("postgresql", postgres)):
                        run(f"full-migrate-{dialect}", [sys.executable, "web/e2e/pas_runner.py", "database", "migrate"],
                            dict(env, PAS_DATABASE_URL=url))
                    run_env = dict(env, PAS_TEST_MYSQL_URL=mysql, PAS_TEST_MYSQL_DATABASE_OK="1",
                                   PAS_TEST_POSTGRES_URL=postgres, PAS_TEST_POSTGRES_SCHEMA_OK="1",
                                   PAS_TEST_SCALABILITY_DATABASE_OK="1", PAS_TEST_SCALABILITY_DATABASE_URL=mysql)
                    run_env.update(integration)
                    junit = output / "backend-full.xml"
                    argv = [sys.executable, "-m", "pytest", "-q", "-rs", f"--junitxml={junit}"]
                    if integration:
                        # A host directory explicitly shared with the owned Docker VM
                        # is needed for the container CLI's bind-mounted output file.
                        temporary_tests = stack.enter_context(tempfile.TemporaryDirectory(prefix="pytest-container-", dir=output))
                        argv.append(f"--basetemp={temporary_tests}")
                    run("backend-full", argv, run_env)
                    tree = ET.parse(junit)
                    results["remaining_skips"] = [{"classname": case.get("classname"), "name": case.get("name"),
                        "reason": case.find("skipped").get("message")} for case in tree.iter("testcase") if case.find("skipped") is not None]
        finally:
            databases.close()
            results["cleanup_complete"] = True
            (output / "local-regression.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
