"""Reject unsafe local rehearsal names, incomplete fences and mismatched backups."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.dev.maintenance_safety import (
    ACCEPTANCE_GATES, owned_name, require_acceptance, require_stopped,
    seal_recovery_set, verify_recovery_set,
)


@pytest.mark.parametrize(
    ("script", "extra"),
    [
        ("run_maintenance_rehearsal.py", []),
        ("run_live_upgrade_rehearsal.py", []),
        ("run_managed_live_upgrade_rehearsal.py", ["--mode", "automatic", "--source-head", "2c3d4e5f6a7b"]),
    ],
)
def test_rehearsal_requires_explicit_source_before_creating_runtime(tmp_path, script, extra):
    output = tmp_path / "evidence"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/dev" / script),
         "--mysql-basedir", str(tmp_path / "mysql"), "--expected-mysql-version", "8.0.13",
         "--output-dir", str(output), *extra],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "--source-ref" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("legacy_ref", [None, "refs/heads/unavailable-legacy-rehearsal-source"])
def test_manual_rehearsal_rejects_missing_legacy_target_before_creating_runtime(tmp_path, legacy_ref):
    output = tmp_path / "evidence"
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/dev/run_managed_live_upgrade_rehearsal.py"),
               "--mysql-basedir", str(tmp_path / "mysql"), "--output-dir", str(output),
               "--mode", "external-manual", "--source-ref", "HEAD", "--source-head", "2c3d4e5f6a7b"]
    if legacy_ref:
        command.extend(["--legacy-target-ref", legacy_ref])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "--legacy-target-ref" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("name", ["pas", "mysql", "pas_maint_public", "pas_maint_" + "a" * 16 + ";DROP DATABASE pas"])
def test_rehearsal_refuses_unowned_database_and_user_names(name):
    with pytest.raises(ValueError):
        owned_name(name)


def test_migration_cannot_begin_with_a_live_old_replica():
    stopped = SimpleNamespace(poll=lambda: 0)
    running = SimpleNamespace(poll=lambda: None)
    with pytest.raises(RuntimeError, match="writers"):
        require_stopped([stopped, running])
    require_stopped([stopped, stopped])
    assert owned_name("pas_maint_" + "a" * 16)


def test_private_server_identity_must_match_the_owned_data_directory(tmp_path):
    from scripts.dev.maintenance_safety import require_owned_datadir

    require_owned_datadir(str(tmp_path) + "/", tmp_path)
    with pytest.raises(ValueError, match="owned"):
        require_owned_datadir("/var/lib/mysql", tmp_path)


@pytest.mark.parametrize("missing", sorted(ACCEPTANCE_GATES))
def test_traffic_cannot_open_before_every_acceptance_gate(missing):
    state = dict.fromkeys(ACCEPTANCE_GATES, True)
    state[missing] = False
    with pytest.raises(RuntimeError, match=missing):
        require_acceptance(state)
    state[missing] = True
    require_acceptance(state)


def recovery_set(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir(mode=0o700)
    for name in ("metadata.sql", "root-key", "runtime.json"):
        target = directory / name
        target.write_text("synthetic " + name)
        target.chmod(0o600)
    seal_recovery_set(directory, source="a" * 40, target="b" * 40)
    return directory


@pytest.mark.parametrize("changed", ["metadata.sql", "root-key", "runtime.json"])
def test_paired_restore_refuses_a_changed_member(tmp_path, changed):
    directory = recovery_set(tmp_path)
    verify_recovery_set(directory, source="a" * 40, target="b" * 40)
    (directory / changed).write_text("wrong recovery member")
    with pytest.raises(ValueError, match="recovery"):
        verify_recovery_set(directory, source="a" * 40, target="b" * 40)


def test_recovery_manifest_must_match_the_pinned_source_and_target(tmp_path):
    directory = recovery_set(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        verify_recovery_set(directory, source="c" * 40, target="b" * 40)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert "key" not in manifest and "password" not in manifest


def test_recovery_set_refuses_symlinks_and_public_permissions(tmp_path):
    directory = recovery_set(tmp_path)
    (directory / "root-key").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        verify_recovery_set(directory, source="a" * 40, target="b" * 40)
    (directory / "root-key").unlink()
    (directory / "root-key").symlink_to(Path(__file__))
    with pytest.raises(ValueError, match="regular"):
        verify_recovery_set(directory, source="a" * 40, target="b" * 40)


def test_source_front_door_cannot_reopen_after_maintenance_begins():
    import httpx
    from scripts.dev.maintenance_proxy import MaintenanceProxy

    proxy = MaintenanceProxy()
    try:
        proxy.start_source(["http://127.0.0.1:1", "http://127.0.0.1:2"])
        proxy.drain()
        assert httpx.get(proxy.url + "/api/agents", trust_env=False).status_code == 503
        with pytest.raises(RuntimeError, match="source"):
            proxy.start_source(["http://127.0.0.1:1", "http://127.0.0.1:2"])
    finally:
        proxy.close()


def test_rollback_traffic_needs_source_restore_checks():
    from scripts.dev.maintenance_proxy import MaintenanceProxy

    proxy = MaintenanceProxy()
    try:
        proxy.drain()
        with pytest.raises(RuntimeError, match="source_schema_checked"):
            proxy.open_rollback(["http://127.0.0.1:1", "http://127.0.0.1:2"],
                                dict.fromkeys(ACCEPTANCE_GATES, True))
        proxy.open_rollback(["http://127.0.0.1:1", "http://127.0.0.1:2"], {
            "source_schema_checked": True, "paired_backup_restored": True,
            "target_writers_stopped": True, "both_replicas_authenticated": True,
        })
    finally:
        proxy.close()
