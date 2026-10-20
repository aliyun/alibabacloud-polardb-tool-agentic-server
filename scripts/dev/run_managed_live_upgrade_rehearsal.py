"""Exercise automatic legacy init, or unpaused init plus a manual executor."""
from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from scripts.dev.run_live_upgrade_rehearsal import LiveRehearsal  # noqa: E402
from scripts.dev.run_maintenance_rehearsal import TARGET_HEAD, query, stop  # noqa: E402

SOURCE_HEADS = ("9c1d2e3f4a5b", "cf4a5b6c7d8e", "2c3d4e5f6a7b")


class ManagedRehearsal(LiveRehearsal):
    def init_command(self):
        prefix = shlex.join([str(self.pythons["108"]), str(ROOT / "scripts/dev/maintenance_app.py"), str(self.sources["108"])])
        return f"{prefix} database migrate && exec {prefix} database check"

    def attempt(self, name, command):
        p = subprocess.run(["/bin/sh", "-c", command], cwd=self.sources["108"], env=self.app_env,
                           capture_output=True, timeout=90)
        (self.output / f"{name}.log").write_bytes(p.stdout + p.stderr)
        return p

    def exercise(self):
        self.app_env.update(PAS_MANAGED_INSTANCE_ID="pa-local108test", PAS_MANAGED_INSTANCE_GENERATION="1")
        self.evidence.update(source_head=self.args.source_head, mode=self.args.mode,
            provider_workflow_tested=False, template_mutations=0, new_provider_values_frozen=True)
        with self.phase("source-baseline-online-backup"):
            self.cli("107", "source-migrate", "database", "migrate")
            self.cli("107", "source-initialize", "initialize-rehearsal")
            a, b = self.start("107", "source-a"), self.start("107", "source-b")
            ca, cb = self.client(a.maintenance_url), self.client(b.maintenance_url)
            self.source_agent = self.create_agent(ca, "Maintenance source retained Agent")
            self.source_token_hash = self.token_hash(cb)
            self.online_backup()
            self.physical_schema("source-physical")
            self.chromium("source-b", b.maintenance_url)
            stop(a)
            if self.args.mode == "external-manual":
                original = self.temporary / "original108"
                original.mkdir()
                archive = subprocess.check_output(["git", "archive", self.args.legacy_target_ref], cwd=ROOT)
                with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
                    contents.extractall(original, filter="data")
                # Same frozen target lock; only the exact original server is loaded.
                assert (original / "uv.lock").read_bytes() == (ROOT / "uv.lock").read_bytes()
                (original / "web/dist").symlink_to(ROOT / "web/dist", target_is_directory=True)
                self.sources["108"] = original
                self.evidence["executed_target_revision"] = self.args.legacy_target_ref
                self.evidence["executed_target_runtime_tree"] = self.git("rev-parse", f"{self.args.legacy_target_ref}:server")
            else:
                self.evidence["executed_target_revision"] = self.target_revision
                self.evidence["executed_target_runtime_tree"] = self.git("rev-parse", "HEAD:server")
        finished, ready, release = threading.Event(), threading.Event(), threading.Event()
        observations, errors, init_codes = [], [], []
        observer = threading.Thread(target=self.monitor, args=(b, cb, finished, observations))
        observer.start()

        def hold():
            try:
                asyncio.run(self.hold_reader(ready, release))
            except Exception as error:
                errors.append(type(error).__name__)

        init_done, first_attempt = threading.Event(), threading.Event()

        def ordinary_retries():
            try:
                for number in range(60):
                    p = self.attempt(f"ordinary-init-{number}", self.init_command())
                    content = p.stdout + p.stderr
                    code = ("SUCCEEDED" if p.returncode == 0 else "BUSY" if b"DATABASE_MIGRATION_BUSY" in content
                            else "MAINTENANCE_REQUIRED" if b"DATABASE_MAINTENANCE_REQUIRED" in content else "UNEXPECTED")
                    init_codes.append(code)
                    first_attempt.set()
                    if code == "SUCCEEDED":
                        init_done.set()
                        return
                    assert code in {"BUSY", "MAINTENANCE_REQUIRED"}, "Unexpected ordinary init failure"
                    time.sleep(.1)
                raise TimeoutError("Ordinary init did not recover")
            except Exception as error:
                errors.append(str(error))

        holder, retries = threading.Thread(target=hold), None
        try:
            with self.phase("unchanged-init-concurrent-migration-and-live-peer"):
                holder.start()
                assert ready.wait(10)
                if self.args.mode == "external-manual":
                    retries = threading.Thread(target=ordinary_retries)
                    retries.start()
                    assert first_attempt.wait(10) and init_codes[0] == "MAINTENANCE_REQUIRED"
                    prefix = shlex.join([str(self.pythons["108"]), str(ROOT / "scripts/dev/maintenance_app.py"), str(self.sources["108"])])
                    command = f"{prefix} database migrate --maintenance --operation-id {shlex.quote(self.operation)}"
                else:
                    command = self.init_command()
                attempt = 0
                while True:
                    handle = (self.output / f"migration-{attempt}.log").open("w")
                    self.handles.append(handle)
                    process = subprocess.Popen(["/bin/sh", "-c", command], env=self.app_env, cwd=self.sources["108"],
                                               stdout=handle, stderr=subprocess.STDOUT)
                    self.processes.append(process)
                    deadline = time.monotonic() + 15
                    waiting = False
                    while process.poll() is None and time.monotonic() < deadline:
                        waiting = bool(query(self.admin_url, "SELECT ID FROM information_schema.PROCESSLIST "
                            "WHERE DB=:db AND STATE LIKE '%metadata lock%'", db=self.database))
                        if waiting:
                            break
                        time.sleep(.05)
                    if waiting:
                        break
                    handle.flush()
                    content = (self.output / f"migration-{attempt}.log").read_text()
                    assert self.args.mode == "external-manual" and "DATABASE_MIGRATION_BUSY" in content and attempt < 10, content[-1000:]
                    attempt += 1
                livez = [httpx.get(b.maintenance_url + "/livez", timeout=3, trust_env=False).status_code for _ in range(5)]
                assert livez == [200] * 5 and b.poll() is None
                time.sleep(1)
                release.set()
                holder.join(timeout=10)
                assert process.wait(timeout=60) == 0, "Inspect migration log"
                if retries:
                    retries.join(timeout=45)
                    assert not retries.is_alive() and init_done.is_set(), errors
                    assert "BUSY" in init_codes
                assert not errors and self.head() == TARGET_HEAD, errors
                self.evidence.update(ordinary_init_results=init_codes, liveness_during_ddl=livez)
            with self.phase("mixed-authenticated-check-replay"):
                self.cli("108", "target-check", "database", "check", "--format", "json")
                before = self.physical_schema("target-before-replay")
                self.cli("108", "target-ordinary-replay", "database", "migrate")
                assert before == self.physical_schema("target-after-replay")
                a = self.start("108", "target-a")
                ca = self.client(a.maintenance_url)
                for index, client in enumerate((ca, cb)):
                    self.create_agent(client, f"Mixed authenticated {index}")
                    assert self.token_hash(client) == self.source_token_hash
                self.chromium("mixed-old-b", b.maintenance_url)
                self.chromium("mixed-target-a", a.maintenance_url)
                self.start("107", "source-restart-refused", expected="DATABASE_SCHEMA_CONTRACT_INVALID")
                assert b.poll() is None
                self.evidence["b_survived_without_restart"] = True
                self.evidence["old_image_restart_refused"] = True
        finally:
            release.set()
            if holder.ident:
                holder.join(timeout=10)
            if retries:
                retries.join(timeout=90)
            finished.set()
            observer.join(timeout=5)
            self.evidence["live_peer_observations"] = observations
        assert observations and all(row["peer_alive"] for row in observations)
        with self.phase("serial-b-upgrade"):
            stop(b)
            b = self.start("108", "target-b")
            cb = self.client(b.maintenance_url)
            self.create_agent(cb, "Final target B write")
            assert self.token_hash(cb) == self.source_token_hash
            self.chromium("both-target-b", b.maintenance_url)
            self.evidence["both_targets_authenticated"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", required=True, help="Locally available immutable source revision")
    parser.add_argument("--source-head", choices=SOURCE_HEADS, required=True)
    parser.add_argument("--legacy-target-ref", help="Locally available immutable target revision for external-manual mode")
    parser.add_argument("--mode", choices=("automatic", "external-manual"), required=True)
    parser.add_argument("--mysql-basedir", required=True, type=Path)
    parser.add_argument("--mysql-arch", choices=("x86_64", "arm64"))
    parser.add_argument("--expected-mysql-version", default="8.0.13")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--chrome", action="store_true")
    args = parser.parse_args()
    if args.mode == "external-manual":
        if not args.legacy_target_ref:
            parser.error("--legacy-target-ref is required for external-manual mode")
        try:
            args.legacy_target_ref = subprocess.check_output(
                ["git", "rev-parse", "--verify", "--end-of-options", f"{args.legacy_target_ref}^{{commit}}"],
                cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            parser.error("--legacy-target-ref must resolve to a locally available commit")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / "rehearsal.json").exists():
        parser.error("Use a new output directory")
    with tempfile.TemporaryDirectory(prefix="pas-managed-live-", dir="/tmp") as temporary:
        runner = ManagedRehearsal(args, Path(temporary))
        try:
            with runner.phase("private-setup"):
                runner.setup()
            runner.exercise()
            runner.evidence["passed"] = True
        finally:
            runner.close()
    runner.evidence["runtime_directory_removed"] = not Path(temporary).exists()
    runner.save()
    print("Managed live rehearsal passed", flush=True)


if __name__ == "__main__":
    main()
