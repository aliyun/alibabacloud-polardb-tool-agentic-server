from __future__ import annotations

import io

import yaml

from server.cli import ConfigProtocolClient, apply_declaration


class FakeProtocol(ConfigProtocolClient):
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.revision = 0

    def command(self, body: dict) -> dict:
        self.calls.append(body)
        action = body["action"]
        if action == "describe":
            return {
                "module": {
                    "revision": self.revision,
                    "workflow_state": "SKIPPED",
                    "ui_hints": {
                        "secret_fields": ["client_secret"]
                    },
                }
            }
        if action == "plan":
            return {"plan": {"valid": True, "writes": False}}
        self.revision += 1
        if action == "validate":
            return {
                "module": {"revision": self.revision},
                "validation": {"validation_id": "proof"},
            }
        return {"module": {"revision": self.revision}}


def test_apply_dry_run_only_calls_describe_and_plan(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SSO_SECRET", "secret")
    client = FakeProtocol()
    declaration = {
        "protocol_version": 1,
        "user_sso": {
            "desired_state": "active",
            "config": {
                "client_id": "client",
                "client_secret_from_env": "SSO_SECRET",
            },
        },
    }

    result = apply_declaration(
        client,
        declaration,
        dry_run=True,
        stdin=io.StringIO(),
    )

    assert result["dry_run"] is True
    assert [call["action"] for call in client.calls] == [
        "describe",
        "plan",
    ]


def test_apply_active_runs_draft_validate_activate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SSO_SECRET", "secret")
    client = FakeProtocol()
    declaration = {
        "protocol_version": 1,
        "user_sso": {
            "desired_state": "active",
            "config": {
                "client_id": "client",
                "client_secret_from_env": "SSO_SECRET",
            },
        },
    }

    apply_declaration(
        client,
        declaration,
        dry_run=False,
        stdin=io.StringIO(),
    )

    assert [call["action"] for call in client.calls] == [
        "describe",
        "plan",
        "save_draft",
        "validate",
        "activate",
    ]
    assert "idempotency_key" in client.calls[-1]


def test_export_writes_redacted_yaml(tmp_path) -> None:
    from server.cli import write_export

    target = tmp_path / "current.yaml"
    write_export(
        target,
        {
            "protocol_version": 1,
            "modules": {
                "user_sso": {
                    "config": {"client_id": "client"},
                    "metadata": {
                        "configured_secret_fields": [
                            "client_secret"
                        ]
                    },
                }
            },
        },
    )
    loaded = yaml.safe_load(target.read_text())
    assert loaded["modules"]["user_sso"]["config"] == {
        "client_id": "client"
    }
    assert "$secret" not in target.read_text()
    assert "ciphertext" not in target.read_text()


def test_guided_configure_runs_plan_before_activation(
    monkeypatch,
) -> None:
    from server.cli import _configure_one

    class GuidedProtocol(FakeProtocol):
        def command(self, body: dict) -> dict:
            if body["action"] == "describe":
                self.calls.append(body)
                return {
                    "module": {
                        "revision": 0,
                        "draft": None,
                        "schema": {
                            "properties": {
                                "client_id": {"type": "string"},
                                "client_secret": {"type": "string"},
                            }
                        },
                        "ui_hints": {
                            "secret_fields": ["client_secret"]
                        },
                    }
                }
            return super().command(body)

    answers = iter(["client", "a"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "getpass.getpass", lambda _prompt: "hidden-secret"
    )
    client = GuidedProtocol()

    _configure_one(client, "user_sso")

    assert [call["action"] for call in client.calls] == [
        "describe",
        "plan",
        "save_draft",
        "validate",
        "activate",
    ]
    assert client.calls[1]["config"]["client_secret"] == (
        "hidden-secret"
    )
