"""Rehearse the explicit live-peer exception on an owned local MySQL server.

No production access, Provider mutation, schema reclassification or new PAS
runtime behavior. The old peer deliberately stays alive during the override.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from scripts.dev.live_upgrade_patch import override_command  # noqa: E402
from scripts.dev.run_maintenance_rehearsal import (  # noqa: E402
    Rehearsal, SOURCE_HEAD, TARGET_HEAD, digest, query, stop,
)


class LiveRehearsal(Rehearsal):
    def online_backup(self):
        """Restore only into a distinct owned empty database while B keeps running."""
        argv = [shutil.which("mysqldump"), "--no-defaults", "--protocol=TCP", "--host=127.0.0.1",
            f"--port={self.port}", "--user=root", "--skip-password", "--single-transaction", "--quick",
            "--hex-blob", "--no-tablespaces", "--column-statistics=0", "--set-gtid-purged=OFF", self.database]
        result = subprocess.run(argv, env=self.env, capture_output=True, check=True, timeout=120)
        backup = self.temporary / "online-backup.sql"
        backup.write_bytes(result.stdout)
        backup.chmod(0o600)
        key = self.temporary / "original-key"
        key.write_text(self.app_env["PAS_ENCRYPTION_KEY"])
        key.chmod(0o600)
        probe = self.create_database()
        assert probe != self.database
        subprocess.run([shutil.which("mysql"), "--no-defaults", "--protocol=TCP", "--host=127.0.0.1",
            f"--port={self.port}", "--user=root", "--skip-password", probe], input=result.stdout,
            env=self.env, capture_output=True, check=True, timeout=120)
        probe_env = dict(self.app_env, PAS_DATABASE_URL=f"mysql+asyncmy://root@127.0.0.1:{self.port}/{probe}")
        self.cli("107", "online-backup-restore-check", "database", "check", "--format", "json", env=probe_env)
        restored = self.start_with_env("107", "restored-source", probe_env)
        client = self.client(restored.maintenance_url)
        assert self.token_hash(client) == self.source_token_hash
        stop(restored)
        self.evidence["online_backup"] = {"sha256": digest(result.stdout), "bytes": len(result.stdout),
            "source_restore_and_token_decryption": True, "writes_after_snapshot_excluded": True}

    def start_with_env(self, version, name, env):
        original = self.app_env
        try:
            self.app_env = env
            return self.start(version, name)
        finally:
            self.app_env = original

    def monitor(self, peer, client, finished, observations):
        while not finished.is_set():
            started = time.monotonic()
            try:
                response = client.get("/api/agents", timeout=3)
                status = response.status_code
            except httpx.HTTPError as error:
                status = type(error).__name__
            observations.append({"status": status, "ms": round((time.monotonic() - started) * 1000, 2),
                                 "peer_alive": peer.poll() is None})
            if peer.poll() is not None:
                break
            finished.wait(.05)

    async def hold_reader(self, ready, release):
        engine = create_async_engine(self.url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT id FROM agents LIMIT 1"))
                ready.set()
                if not await asyncio.to_thread(release.wait, 30):
                    raise TimeoutError("Owned test reader release timed out")
                await connection.commit()
        finally:
            await engine.dispose()

    def exercise(self):
        self.evidence.update(exception="explicit live-peer override; not stopped maintenance",
            provider_workflow_tested=False, production_admission="requires pre-release task replay verification",
            new_provider_values_frozen_until_both_target=True, zero_request_failure_guarantee=False)
        with self.phase("source-baseline-and-online-recovery-copy"):
            self.cli("107", "source-migrate", "database", "migrate")
            self.cli("107", "source-initialize", "initialize-rehearsal")
            a, b = self.start("107", "source-a"), self.start("107", "source-b")
            ca, cb = self.client(a.maintenance_url), self.client(b.maintenance_url)
            self.source_agent = self.create_agent(ca, "Maintenance source retained Agent")
            self.source_token_hash = self.token_hash(cb)
            self.physical_schema("source-physical")
            self.online_backup()
            self.chromium("source-b", b.maintenance_url)
            peer_pid = b.pid
            stop(a)  # Only drained replica A is replaced; B is never stopped here.
        with self.phase("ordinary-refusal-and-read-only-failure-keep-b-alive"):
            self.cli("108", "ordinary-refused", "database", "migrate", expected="DATABASE_MAINTENANCE_REQUIRED")
            query(self.admin_url, "SET GLOBAL read_only=1")
            try:
                self.cli("108", "readonly-refused", "database", "migrate", "--maintenance",
                         "--operation-id", self.operation, expected="DATABASE_MIGRATION_READ_ONLY")
                assert cb.get("/api/agents").status_code == 200 and b.poll() is None
            finally:
                query(self.admin_url, "SET GLOBAL read_only=0")
            assert self.head() == SOURCE_HEAD
        observations, finished = [], threading.Event()
        observer = threading.Thread(target=self.monitor, args=(b, cb, finished, observations))
        observer.start()
        try:
            with self.phase("live-peer-ddl-lock-and-liveness") as entry:
                ready, release = threading.Event(), threading.Event()
                holder_errors = []

                def hold():
                    try:
                        asyncio.run(self.hold_reader(ready, release))
                    except Exception as error:
                        holder_errors.append(type(error).__name__)

                holder = threading.Thread(target=hold)
                holder.start()
                try:
                    assert ready.wait(10), "Owned reader did not acquire a transaction"
                    command = override_command(self.operation)
                    handle = (self.output / "live-init-override.log").open("w")
                    self.handles.append(handle)
                    # sh preserves the exact command used by the temporary init.
                    cli = [str(self.pythons['108']), str(ROOT / 'scripts/dev/maintenance_app.py'), str(ROOT)]
                    prefix = " ".join(shlex.quote(part) for part in cli)
                    command = command.replace("pas ", prefix + " ")
                    migration = subprocess.Popen(["/bin/sh", "-c", command], env=self.app_env,
                        cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
                    self.processes.append(migration)
                    deadline = time.monotonic() + 20
                    while True:
                        waiting = query(self.admin_url,
                            "SELECT STATE AS state FROM information_schema.PROCESSLIST "
                            "WHERE DB=:db AND STATE LIKE '%metadata lock%'", db=self.database)
                        if waiting:
                            break
                        assert migration.poll() is None, "Migration exited before the lock probe"
                        if time.monotonic() > deadline:
                            raise TimeoutError("Expected DDL metadata-lock wait not observed")
                        time.sleep(.05)
                    liveness = []
                    for _ in range(5):
                        liveness.append(httpx.get(b.maintenance_url + "/livez", timeout=3, trust_env=False).status_code)
                        time.sleep(.2)
                    assert liveness == [200] * 5 and b.poll() is None
                    entry.update(ddl_metadata_lock_observed=True, livez=liveness, peer_pid=peer_pid)
                finally:
                    release.set()
                    holder.join(timeout=10)
                assert not holder.is_alive() and not holder_errors, holder_errors
                assert migration.wait(timeout=60) == 0, "Inspect live-init-override.log"
                assert self.head() == TARGET_HEAD
            with self.phase("restore-original-init-and-mixed-authenticated-writes"):
                before = self.physical_schema("target-before-original-init")
                self.cli("108", "restored-ordinary-init", "database", "migrate")
                self.cli("108", "restored-ordinary-check", "database", "check", "--format", "json")
                assert before == self.physical_schema("target-after-original-init")
                a = self.start("108", "target-a")
                ca = self.client(a.maintenance_url)
                self.create_agent(cb, "Live old peer write after migration")
                self.create_agent(ca, "Live target peer write during mixed window")
                for client in (ca, cb):
                    assert self.token_hash(client) == self.source_token_hash
                    for path in ("/api/users", "/api/agents", "/api/identity-sources"):
                        assert client.get(path).status_code == 200, path
                self.chromium("mixed-old-b", b.maintenance_url)
                self.chromium("mixed-target-a", a.maintenance_url)
                self.start("107", "old-restart-refused", expected="DATABASE_SCHEMA_CONTRACT_INVALID")
                assert b.pid == peer_pid and b.poll() is None
        finally:
            finished.set()
            observer.join(timeout=5)
            assert not observer.is_alive()
            self.evidence["live_peer_observations"] = observations
        assert observations and all(row['peer_alive'] for row in observations)
        self.evidence["b_survived_migration_and_original_init"] = b.pid == peer_pid and b.poll() is None
        self.evidence["b_restarts_before_planned_upgrade"] = 0
        with self.phase("serial-b-upgrade-and-two-target-acceptance"):
            assert self.evidence["b_survived_migration_and_original_init"]
            stop(b)  # Planned B upgrade only after authenticated A is available.
            b = self.start("108", "target-b")
            cb = self.client(b.maintenance_url)
            for index, client in enumerate((ca, cb)):
                self.create_agent(client, f"Both target {index} authenticated write")
                assert self.token_hash(client) == self.source_token_hash
            self.chromium("both-target-b", b.maintenance_url)
            self.evidence["both_targets_authenticated"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mysql-basedir", required=True, type=Path)
    parser.add_argument("--mysql-arch", choices=("x86_64", "arm64"))
    parser.add_argument("--expected-mysql-version", required=True)
    parser.add_argument("--source-ref", required=True, help="Locally available immutable source revision with the required schema head")
    parser.add_argument("--chrome", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / "rehearsal.json").exists():
        parser.error("Use a new output directory to preserve evidence")
    with tempfile.TemporaryDirectory(prefix="pas-live-108-", dir="/tmp") as temporary:
        runner = LiveRehearsal(args, Path(temporary))
        try:
            with runner.phase("private-environment-setup"):
                runner.setup()
            runner.exercise()
            runner.evidence["passed"] = True
        finally:
            runner.close()
    runner.evidence["runtime_directory_removed"] = not Path(temporary).exists()
    runner.save()
    print("Live-peer rehearsal passed; private runtime removed", flush=True)


if __name__ == "__main__":
    main()
