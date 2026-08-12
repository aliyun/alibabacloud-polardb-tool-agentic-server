from __future__ import annotations

import io
import json

import pytest

from server.cli import (
    CLIError,
    build_parser,
    resolve_declaration_secrets,
    resolve_token,
)


def test_parser_exposes_guided_configuration_commands() -> None:
    parser = build_parser()
    for argv in (
        ["config", "init"],
        ["config", "modules"],
        ["config", "configure", "user_sso"],
        ["config", "resume", "user_sso"],
        ["config", "skip", "user_sso"],
        ["config", "disable", "user_sso"],
        ["config", "apply", "--file", "onboarding.yaml"],
        ["config", "export", "--file", "current.yaml"],
        [
            "config",
            "bootstrap-token",
            "issue",
            "--output",
            "/tmp/token",
        ],
    ):
        assert parser.parse_args(argv).handler


def test_plaintext_bootstrap_token_option_does_not_exist() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["config", "modules", "--bootstrap-token", "secret"]
        )


def test_resolve_token_requires_exactly_one_source(
    monkeypatch, tmp_path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-file")
    token_file.chmod(0o600)
    monkeypatch.setenv("PAS_BOOTSTRAP_TOKEN", "from-env")

    with pytest.raises(CLIError, match="exactly one"):
        resolve_token(
            token_file=str(token_file),
            token_stdin=True,
            stdin=io.StringIO("from-stdin"),
        )


def test_secret_references_are_resolved_and_plaintext_rejected(
    monkeypatch, tmp_path
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-secret\n")
    secret_file.chmod(0o600)
    monkeypatch.setenv("CLIENT_SECRET", "env-secret")
    config = {
        "client_id": "client",
        "client_secret_from_env": "CLIENT_SECRET",
    }
    assert resolve_declaration_secrets(
        config,
        secret_fields={"client_secret"},
        stdin=io.StringIO(),
    ) == {
        "client_id": "client",
        "client_secret": "env-secret",
    }

    with pytest.raises(CLIError, match="plaintext"):
        resolve_declaration_secrets(
            {"client_secret": "not-allowed"},
            secret_fields={"client_secret"},
            stdin=io.StringIO(),
        )


def test_secret_reference_sources_are_mutually_exclusive(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    with pytest.raises(CLIError, match="exactly one"):
        resolve_declaration_secrets(
            {
                "client_secret_from_env": "CLIENT_SECRET",
                "client_secret_from_stdin": True,
            },
            secret_fields={"client_secret"},
            stdin=io.StringIO("stdin-secret"),
        )


def test_nested_secret_references_resolve_only_declared_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STS_AK", "ak")
    monkeypatch.setenv("STS_SK", "sk")

    resolved = resolve_declaration_secrets(
        {
            "assume_role": {
                "source_access_key_id_from_env": "STS_AK",
                "source_access_key_secret_from_env": "STS_SK",
                "note_from_env": "NOT_A_SECRET",
            }
        },
        secret_fields={
            "assume_role.source_access_key_id",
            "assume_role.source_access_key_secret",
        },
        stdin=io.StringIO(),
    )

    assert resolved == {
        "assume_role": {
            "source_access_key_id": "ak",
            "source_access_key_secret": "sk",
            "note_from_env": "NOT_A_SECRET",
        }
    }


@pytest.mark.parametrize(
    ("reference", "value", "message"),
    [
        (
            "source_access_key_id_from_env",
            "",
            "source_access_key_id_from_env must name a variable",
        ),
        (
            "source_access_key_id_from_file",
            "",
            "secret file path must be absolute",
        ),
        (
            "source_access_key_id_from_stdin",
            False,
            "source_access_key_id_from_stdin must be true",
        ),
    ],
)
def test_nested_secret_reference_rejects_present_invalid_source(
    reference, value, message
) -> None:
    with pytest.raises(CLIError, match=message):
        resolve_declaration_secrets(
            {"assume_role": {reference: value}},
            secret_fields={"assume_role.source_access_key_id"},
            stdin=io.StringIO(),
        )


def test_nested_secret_reference_rejects_false_second_source(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STS_AK", "ak")

    with pytest.raises(CLIError, match="exactly one"):
        resolve_declaration_secrets(
            {
                "assume_role": {
                    "source_access_key_id_from_env": "STS_AK",
                    "source_access_key_id_from_stdin": False,
                }
            },
            secret_fields={"assume_role.source_access_key_id"},
            stdin=io.StringIO(),
        )


def test_nested_stdin_secret_references_follow_declaration_order() -> None:
    assert resolve_declaration_secrets(
        {
            "assume_role": {
                "source_access_key_id_from_stdin": True,
                "source_access_key_secret_from_stdin": True,
            }
        },
        secret_fields={
            "assume_role.source_access_key_id",
            "assume_role.source_access_key_secret",
        },
        stdin=io.StringIO("first-line\nsecond-line\n"),
    ) == {
        "assume_role": {
            "source_access_key_id": "first-line",
            "source_access_key_secret": "second-line",
        }
    }


def test_plaintext_is_allowed_for_undeclared_suffix_looking_field() -> None:
    assert resolve_declaration_secrets(
        {
            "assume_role": {
                "source_access_key_id": "plaintext-is-not-declared",
                "role_session_name": "pas-session",
            }
        },
        secret_fields={"assume_role.source_access_key_secret"},
        stdin=io.StringIO(),
    ) == {
        "assume_role": {
            "source_access_key_id": "plaintext-is-not-declared",
            "role_session_name": "pas-session",
        }
    }


def test_json_output_is_stable(capsys) -> None:
    from server.cli import print_output

    print_output({"z": 1, "a": 2}, output="json")
    assert json.loads(capsys.readouterr().out) == {"a": 2, "z": 1}
