from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_only_two_server_environment_names_are_documented():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert set(re.findall(r"^(PAS_[A-Z0-9_]+)=", text, re.MULTILINE)) == {
        "PAS_DATABASE_URL",
        "PAS_ENCRYPTION_KEY",
    }


def test_legacy_application_configuration_is_removed():
    assert not (ROOT / "config.example.yaml").exists()
    assert not (ROOT / "server/models/system_setting.py").exists()
    assert not (ROOT / "server/core/settings_manager.py").exists()
    assert not (ROOT / "server/api/settings.py").exists()

    config_source = (ROOT / "server/config.py").read_text(encoding="utf-8")
    assert "yaml.safe_load" not in config_source
    assert "load_config" not in config_source
    assert "_apply_env_overrides" not in config_source


def test_legacy_settings_route_is_absent():
    router_source = (ROOT / "server/api/router.py").read_text(encoding="utf-8")
    assert "settings_router" not in router_source
    assert "/api/settings" not in router_source


def test_runtime_server_code_has_no_legacy_settings_imports():
    offenders: list[str] = []
    for path in (ROOT / "server").rglob("*.py"):
        if "db/migrations/versions" in path.as_posix():
            continue
        source = path.read_text(encoding="utf-8")
        if (
            "SystemSetting" in source
            or "SETTINGS_SCHEMA" in source
            or "settings_manager" in source
        ):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
