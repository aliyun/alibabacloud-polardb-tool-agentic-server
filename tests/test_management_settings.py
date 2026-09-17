from __future__ import annotations

from pathlib import Path

import pytest

from server.management.settings import (
    ListenerConfigError,
    load_listener_settings,
)


def test_defaults_business_port_and_disables_management() -> None:
    settings = load_listener_settings({})

    assert settings.business_port == 18760
    assert settings.management_port is None
    assert settings.auth_mode is None
    assert settings.token is None
    assert settings.managed_identity is None
    assert settings.listen_host == "0.0.0.0"
    assert settings.local_sso_dev_mode is False


def test_local_sso_dev_mode_binds_only_to_ipv4_loopback() -> None:
    settings = load_listener_settings({}, local_sso_dev_mode=True)

    assert settings.listen_host == "127.0.0.1"
    assert settings.local_sso_dev_mode is True


@pytest.mark.parametrize("port", ["1", "18760", "65535"])
def test_accepts_business_port_boundaries(port: str) -> None:
    settings = load_listener_settings({"PAS_SERVER_PORT": port})

    assert settings.business_port == int(port)


@pytest.mark.parametrize("name", ["PAS_SERVER_PORT", "PAS_MANAGEMENT_PORT"])
@pytest.mark.parametrize("port", ["", "0", "65536", "not-a-port"])
def test_rejects_invalid_ports(name: str, port: str) -> None:
    env = {name: port}
    if name == "PAS_MANAGEMENT_PORT":
        env["PAS_MANAGEMENT_AUTH_MODE"] = "bearer-token"

    with pytest.raises(ListenerConfigError, match=name):
        load_listener_settings(env)


def test_rejects_conflicting_listener_ports() -> None:
    with pytest.raises(ListenerConfigError, match="distinct"):
        load_listener_settings(
            {
                "PAS_SERVER_PORT": "18080",
                "PAS_MANAGEMENT_PORT": "18080",
                "PAS_MANAGEMENT_AUTH_MODE": "trusted-network",
                "PAS_MANAGED_INSTANCE_ID": "pmcp-test",
                "PAS_MANAGED_INSTANCE_GENERATION": "1",
            }
        )


def test_requires_auth_mode_when_management_is_enabled() -> None:
    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_AUTH_MODE"):
        load_listener_settings({"PAS_MANAGEMENT_PORT": "18081"})


def test_rejects_auth_mode_without_management_listener() -> None:
    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_PORT"):
        load_listener_settings(
            {"PAS_MANAGEMENT_AUTH_MODE": "trusted-network"}
        )


@pytest.mark.parametrize("auth_mode", ["", "none", "basic", "TRUSTED-NETWORK"])
def test_rejects_unknown_auth_modes(auth_mode: str) -> None:
    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_AUTH_MODE"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": auth_mode,
            }
        )


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"PAS_MANAGED_INSTANCE_ID": "pmcp-test"},
        {"PAS_MANAGED_INSTANCE_GENERATION": "1"},
        {
            "PAS_MANAGED_INSTANCE_ID": " ",
            "PAS_MANAGED_INSTANCE_GENERATION": "1",
        },
        {
            "PAS_MANAGED_INSTANCE_ID": "pmcp-test",
            "PAS_MANAGED_INSTANCE_GENERATION": "0",
        },
        {
            "PAS_MANAGED_INSTANCE_ID": "pmcp-test",
            "PAS_MANAGED_INSTANCE_GENERATION": "not-an-integer",
        },
    ],
)
def test_trusted_network_requires_complete_managed_identity(
    identity: dict[str, str],
) -> None:
    with pytest.raises(ListenerConfigError, match="PAS_MANAGED_INSTANCE"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": "trusted-network",
                **identity,
            }
        )


def test_loads_trusted_network_identity_without_token() -> None:
    settings = load_listener_settings(
        {
            "PAS_SERVER_PORT": "18080",
            "PAS_MANAGEMENT_PORT": "18081",
            "PAS_MANAGEMENT_AUTH_MODE": "trusted-network",
            "PAS_MANAGED_INSTANCE_ID": " pmcp-test ",
            "PAS_MANAGED_INSTANCE_GENERATION": "2",
        }
    )

    assert settings.business_port == 18080
    assert settings.management_port == 18081
    assert settings.auth_mode == "trusted-network"
    assert settings.token is None
    assert settings.managed_identity.instance_id == "pmcp-test"
    assert settings.managed_identity.generation == 2


def test_reads_bearer_token_from_restricted_absolute_file(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "management.token"
    token_file.write_text("secret-token\n")
    token_file.chmod(0o600)

    settings = load_listener_settings(
        {
            "PAS_MANAGEMENT_PORT": "18081",
            "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
            "PAS_MANAGEMENT_TOKEN_FILE": str(token_file),
        }
    )

    assert settings.auth_mode == "bearer-token"
    assert settings.token == b"secret-token"
    assert settings.managed_identity is None


def test_listener_settings_repr_does_not_expose_bearer_token(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "management.token"
    token_file.write_text("secret-token")
    token_file.chmod(0o600)

    settings = load_listener_settings(
        {
            "PAS_MANAGEMENT_PORT": "18081",
            "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
            "PAS_MANAGEMENT_TOKEN_FILE": str(token_file),
        }
    )

    assert "secret-token" not in repr(settings)


def test_allows_projected_bearer_token_symlink(tmp_path: Path) -> None:
    version_dir = tmp_path / "..2026_08_03"
    version_dir.mkdir()
    target = version_dir / "management.token"
    target.write_text("secret-token")
    target.chmod(0o600)
    link = tmp_path / "management.token"
    link.symlink_to(target)

    settings = load_listener_settings(
        {
            "PAS_MANAGEMENT_PORT": "18081",
            "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
            "PAS_MANAGEMENT_TOKEN_FILE": str(link),
        }
    )

    assert settings.token == b"secret-token"


@pytest.mark.parametrize(
    "token_file_value",
    [None, "", "relative/management.token"],
)
def test_rejects_missing_empty_or_relative_bearer_token_file(
    token_file_value: str | None,
) -> None:
    env = {
        "PAS_MANAGEMENT_PORT": "18081",
        "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
    }
    if token_file_value is not None:
        env["PAS_MANAGEMENT_TOKEN_FILE"] = token_file_value

    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_TOKEN_FILE"):
        load_listener_settings(env)


def test_rejects_bearer_token_file_with_broad_permissions(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "management.token"
    token_file.write_text("secret-token")
    token_file.chmod(0o640)

    with pytest.raises(ListenerConfigError, match="permissions"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
                "PAS_MANAGEMENT_TOKEN_FILE": str(token_file),
            }
        )


def test_rejects_empty_bearer_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "management.token"
    token_file.write_text(" \n")
    token_file.chmod(0o600)

    with pytest.raises(ListenerConfigError, match="empty"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
                "PAS_MANAGEMENT_TOKEN_FILE": str(token_file),
            }
        )


def test_rejects_plaintext_bearer_token_environment_value() -> None:
    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_TOKEN"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": "bearer-token",
                "PAS_MANAGEMENT_TOKEN": "must-not-be-accepted",
            }
        )


def test_rejects_token_file_in_trusted_network_mode(tmp_path: Path) -> None:
    token_file = tmp_path / "management.token"
    token_file.write_text("secret-token")
    token_file.chmod(0o600)

    with pytest.raises(ListenerConfigError, match="PAS_MANAGEMENT_TOKEN_FILE"):
        load_listener_settings(
            {
                "PAS_MANAGEMENT_PORT": "18081",
                "PAS_MANAGEMENT_AUTH_MODE": "trusted-network",
                "PAS_MANAGED_INSTANCE_ID": "pmcp-test",
                "PAS_MANAGED_INSTANCE_GENERATION": "1",
                "PAS_MANAGEMENT_TOKEN_FILE": str(token_file),
            }
        )
