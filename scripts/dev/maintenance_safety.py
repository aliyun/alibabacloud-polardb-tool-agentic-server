"""Safety boundaries shared by the disposable maintenance rehearsal."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

ACCEPTANCE_GATES = frozenset({
    "old_writers_stopped", "backup_restore_verified", "migration_checked",
    "replay_checked", "physical_data_verified", "both_replicas_authenticated",
})
ROLLBACK_GATES = frozenset({"source_schema_checked", "paired_backup_restored",
                           "target_writers_stopped", "both_replicas_authenticated"})
RECOVERY_FILES = ("metadata.sql", "root-key", "runtime.json")


def owned_name(name: str) -> str:
    if not re.fullmatch(r"pas_maint_[0-9a-f]{16}", name):
        raise ValueError("Only random owned maintenance database/user names are allowed")
    return name


def require_owned_datadir(observed: str, expected: Path) -> None:
    if Path(observed).resolve() != expected.resolve():
        raise ValueError("MySQL must use the owned private rehearsal data directory")


def require_stopped(processes) -> None:
    if any(process.poll() is None for process in processes):
        raise RuntimeError("Stop all old readers/writers before migration or restoration")


def require_acceptance(state: dict, gates=ACCEPTANCE_GATES) -> None:
    missing = sorted(gate for gate in gates if state.get(gate) is not True)
    if missing:
        raise RuntimeError("Keep maintenance traffic closed: " + ", ".join(missing))


def private_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Recovery member must be a regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError("Recovery members must be private")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def seal_recovery_set(directory: Path, *, source: str, target: str) -> dict:
    manifest = {"source": source, "target": target,
                "files": {name: private_file(directory / name) for name in RECOVERY_FILES}}
    file = directory / "manifest.json"
    file.write_text(json.dumps(manifest, indent=2) + "\n")
    file.chmod(0o600)
    return manifest


def verify_recovery_set(directory: Path, *, source: str, target: str) -> dict:
    private_file(directory / "manifest.json")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("source") != source or manifest.get("target") != target:
        raise ValueError("Recovery identity does not match pinned source/target")
    expected = manifest.get("files", {})
    if set(expected) != set(RECOVERY_FILES):
        raise ValueError("Incomplete recovery set")
    if any(private_file(directory / name) != expected[name] for name in RECOVERY_FILES):
        raise ValueError("Mismatched recovery member")
    return manifest
