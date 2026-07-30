from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, TextIO

import httpx
import yaml

from server.db.schema import DatabaseSchemaError
from server.deployment.env_generator import (
    EnvironmentGenerationError,
    read_masked_secret,
)


class CLIError(ValueError):
    pass


def _restricted_file(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise CLIError("secret file path must be absolute")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CLIError("secret file must be a regular non-symlink")
    if info.st_mode & 0o077:
        raise CLIError("secret file must not be accessible by group or others")
    return path


def resolve_token(
    *,
    token_file: str | None,
    token_stdin: bool,
    stdin: TextIO,
) -> str | None:
    env_token = os.environ.get("PAS_BOOTSTRAP_TOKEN")
    sources = [
        bool(env_token),
        token_file is not None,
        token_stdin,
    ]
    if sum(sources) > 1:
        raise CLIError(
            "bootstrap token must come from exactly one source"
        )
    if env_token:
        return env_token
    if token_file is not None:
        return _restricted_file(token_file).read_text().strip()
    if token_stdin:
        return stdin.readline().strip()
    return None


def _read_secret_reference(
    field: str,
    config: dict[str, Any],
    *,
    stdin: TextIO,
) -> str | None:
    references = {
        "env": config.get(f"{field}_from_env"),
        "file": config.get(f"{field}_from_file"),
        "stdin": config.get(f"{field}_from_stdin"),
    }
    selected = [
        kind for kind, value in references.items() if value
    ]
    if len(selected) > 1:
        raise CLIError(
            f"{field} must use exactly one secret source"
        )
    if not selected:
        return None
    source = selected[0]
    if source == "env":
        variable = references[source]
        if not isinstance(variable, str) or not variable:
            raise CLIError(f"{field}_from_env must name a variable")
        value = os.environ.get(variable)
        if value is None:
            raise CLIError(
                f"environment variable for {field} is not set"
            )
        return value
    if source == "file":
        value = references[source]
        if not isinstance(value, str):
            raise CLIError(f"{field}_from_file must be a path")
        return _restricted_file(value).read_text().rstrip("\r\n")
    if references[source] is not True:
        raise CLIError(f"{field}_from_stdin must be true")
    return stdin.readline().rstrip("\r\n")


def resolve_declaration_secrets(
    config: dict[str, Any],
    *,
    secret_fields: set[str],
    stdin: TextIO,
) -> dict[str, Any]:
    result = {
        key: value
        for key, value in config.items()
        if not key.endswith(
            ("_from_env", "_from_file", "_from_stdin")
        )
    }
    for field in secret_fields:
        if field in result:
            raise CLIError(
                f"plaintext secret field '{field}' is forbidden"
            )
        value = _read_secret_reference(field, config, stdin=stdin)
        if value is not None:
            result[field] = value
    for key in config:
        if key.endswith(("_from_env", "_from_file", "_from_stdin")):
            field = key.rsplit("_from_", 1)[0]
            if field not in secret_fields:
                raise CLIError(
                    f"secret reference used for non-secret field '{field}'"
                )
    return result


class ConfigProtocolClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18760",
        *,
        bootstrap_token: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bootstrap_token = bootstrap_token
        self.bearer_token = bearer_token
        self.timeout = timeout

    def command(self, body: dict[str, Any]) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self.bootstrap_token:
            headers["Authorization"] = (
                f"Bootstrap {self.bootstrap_token}"
            )
        elif self.bearer_token:
            headers["Authorization"] = (
                f"Bearer {self.bearer_token}"
            )
        response = httpx.post(
            f"{self.base_url}/api/config",
            json={"protocol_version": 1, **body},
            headers=headers,
            timeout=self.timeout,
        )
        if response.is_error:
            try:
                detail = response.json().get("detail", {})
                message = detail.get("message") or str(detail)
            except ValueError:
                message = f"HTTP {response.status_code}"
            raise CLIError(message)
        value: dict[str, Any] = response.json()
        return value


def _module_secret_fields(description: dict[str, Any]) -> set[str]:
    module = description["module"]
    hints = module.get("ui_hints", {})
    return set(hints.get("secret_fields", []))


def apply_declaration(
    client: ConfigProtocolClient,
    declaration: dict[str, Any],
    *,
    dry_run: bool,
    stdin: TextIO,
) -> dict[str, Any]:
    if declaration.get("protocol_version") != 1:
        raise CLIError("protocol_version must be 1")
    from server.configuration.registry import (
        MODULE_REGISTRY,
        topological_modules,
    )

    results: dict[str, Any] = {}
    for module in topological_modules(MODULE_REGISTRY):
        if module not in declaration:
            continue
        desired = declaration[module]
        if not isinstance(desired, dict):
            raise CLIError(f"{module} declaration must be an object")
        description = client.command(
            {"action": "describe", "module": module}
        )
        revision = int(description["module"]["revision"])
        secret_fields = _module_secret_fields(description)
        config = resolve_declaration_secrets(
            desired.get("config", {}),
            secret_fields=secret_fields,
            stdin=stdin,
        )
        admin_password = (
            config.pop("password", None)
            if module == "core_admin"
            else None
        )
        desired_state = str(
            desired.get("desired_state", "active")
        ).lower()
        if desired_state == "skipped":
            if not dry_run:
                results[module] = client.command(
                    {
                        "action": "skip",
                        "module": module,
                        "expected_revision": revision,
                    }
                )
            continue
        if desired_state == "disabled":
            if not dry_run:
                results[module] = client.command(
                    {
                        "action": "disable",
                        "module": module,
                        "expected_revision": revision,
                        "idempotency_key": secrets.token_urlsafe(18),
                    }
                )
            continue
        plan = client.command(
            {
                "action": "plan",
                "module": module,
                "config": config,
            }
        )
        results[module] = plan
        if dry_run:
            continue
        saved = client.command(
            {
                "action": "save_draft",
                "module": module,
                "expected_revision": revision,
                "config": config,
            }
        )
        validated = client.command(
            {
                "action": "validate",
                "module": module,
                "expected_revision": saved["module"]["revision"],
            }
        )
        activation: dict[str, Any] = {
            "action": "activate",
            "module": module,
            "expected_revision": validated["module"]["revision"],
            "validation_id": validated["validation"][
                "validation_id"
            ],
            "idempotency_key": secrets.token_urlsafe(18),
        }
        if module == "core_admin":
            if admin_password is None:
                admin_password = _read_secret_reference(
                    "password",
                    desired.get("config", {}),
                    stdin=stdin,
                )
            activation["config"] = {"password": admin_password}
        results[module] = client.command(activation)
    return {"dry_run": dry_run, "modules": results}


def write_export(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(
            value,
            sort_keys=False,
            allow_unicode=True,
        )
    )


def print_output(value: Any, *, output: str) -> None:
    if output == "json":
        print(
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    print(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))


def _add_remote_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get(
            "PAS_SERVER_URL", "http://127.0.0.1:18760"
        ),
    )
    parser.add_argument(
        "--output", choices=("human", "json"), default="human"
    )
    parser.add_argument(
        "--bootstrap-token-stdin", action="store_true"
    )
    parser.add_argument("--bootstrap-token-file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pas")
    root = parser.add_subparsers(dest="root_command", required=True)
    serve = root.add_parser("serve")
    serve.set_defaults(handler=_handle_serve)
    database = root.add_parser("database")
    database_commands = database.add_subparsers(
        dest="database_command", required=True
    )
    database_check = database_commands.add_parser("check")
    database_check.set_defaults(handler=_handle_database_check)
    database_migrate = database_commands.add_parser("migrate")
    database_migrate.set_defaults(handler=_handle_database_migrate)
    database_create_env = database_commands.add_parser("create-env")
    database_create_env.add_argument("--output", required=True)
    database_create_env.add_argument(
        "--skip-connection-test",
        action="store_true",
    )
    database_create_env.add_argument("--image")
    database_create_env.set_defaults(
        handler=_handle_database_create_env
    )
    config = root.add_parser("config")
    _add_remote_options(config)
    commands = config.add_subparsers(
        dest="config_command", required=True
    )
    for name, handler in (
        ("init", _handle_init),
        ("modules", _handle_modules),
        ("show", _handle_show),
        ("configure", _handle_configure),
        ("resume", _handle_configure),
        ("skip", _handle_state_action),
        ("disable", _handle_state_action),
    ):
        command = commands.add_parser(name)
        if name in {"show", "skip", "disable"}:
            command.add_argument("module")
        elif name in {"configure", "resume"}:
            command.add_argument("module", nargs="?")
        command.set_defaults(handler=handler)
    apply = commands.add_parser("apply")
    apply.add_argument("--file", required=True)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(handler=_handle_apply)
    export = commands.add_parser("export")
    export.add_argument("--file", required=True)
    export.add_argument("--module")
    export.set_defaults(handler=_handle_export)
    bootstrap = commands.add_parser("bootstrap-token")
    bootstrap_commands = bootstrap.add_subparsers(
        dest="bootstrap_command", required=True
    )
    issue = bootstrap_commands.add_parser("issue")
    issue.add_argument("--output", required=True)
    issue.set_defaults(handler=_handle_bootstrap_issue)
    return parser


def _client(args: argparse.Namespace) -> ConfigProtocolClient:
    token = resolve_token(
        token_file=args.bootstrap_token_file,
        token_stdin=args.bootstrap_token_stdin,
        stdin=sys.stdin,
    )
    if token is None and sys.stdin.isatty():
        token = getpass.getpass(
            "Bootstrap token (leave empty for admin auth): "
        )
    return ConfigProtocolClient(
        args.url,
        bootstrap_token=token or None,
        bearer_token=os.environ.get("PAS_ADMIN_TOKEN"),
    )


def _handle_serve(_args: argparse.Namespace) -> Any:
    from server.__main__ import main as serve

    return serve()


def _handle_database_check(_args: argparse.Namespace) -> None:
    from server.db.schema import check_database_schema

    revision = asyncio.run(check_database_schema())
    print(f"Database schema is current: {revision}")


def _handle_database_migrate(_args: argparse.Namespace) -> None:
    from server.db.schema import migrate_database

    migrate_database()
    print("Database migration completed.")


def _handle_database_create_env(args: argparse.Namespace) -> None:
    from server.deployment.env_generator import (
        create_environment_file,
    )

    output = Path(args.output)
    asyncio.run(
        create_environment_file(
            output=output,
            skip_connection_test=args.skip_connection_test,
            image=args.image,
            input_stream=sys.stdin,
            output_stream=sys.stdout,
            secret_reader=read_masked_secret,
        )
    )
    print(f"Environment file created at {output}")


def _handle_modules(args: argparse.Namespace) -> None:
    print_output(
        _client(args).command({"action": "describe"}),
        output=args.output,
    )


def _handle_show(args: argparse.Namespace) -> None:
    print_output(
        _client(args).command(
            {"action": "describe", "module": args.module}
        ),
        output=args.output,
    )


def _choose_module(client: ConfigProtocolClient) -> str:
    described = client.command({"action": "describe"})
    names = [module["name"] for module in described["modules"]]
    for index, name in enumerate(names, 1):
        print(f"{index}. {name}")
    selected = int(input("Module: "))
    if selected < 1 or selected > len(names):
        raise CLIError("invalid module selection")
    return names[selected - 1]


def _interactive_candidate(
    description: dict[str, Any],
) -> dict[str, Any]:
    module = description["module"]
    properties = module["schema"].get("properties", {})
    secret_fields = _module_secret_fields(description)
    candidate: dict[str, Any] = {}
    for field, schema in properties.items():
        current = (module.get("draft") or {}).get(field)
        default = current if current is not None else schema.get("default")
        suffix = f" [{default}]" if default is not None else ""
        if field in secret_fields:
            value = getpass.getpass(f"{field}{suffix}: ")
        else:
            value = input(f"{field}{suffix}: ")
        if not value and default is not None:
            value = default
        if value != "":
            candidate[field] = value
    return candidate


def _configure_one(
    client: ConfigProtocolClient,
    module: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    description = client.command(
        {"action": "describe", "module": module}
    )
    candidate = _interactive_candidate(description)
    admin_password = (
        candidate.pop("password", None)
        if module == "core_admin"
        else None
    )
    plan = client.command(
        {"action": "plan", "module": module, "config": candidate}
    )
    print_output(plan, output="human")
    choice = input("Activate, save draft, or cancel? [a/s/c]: ").lower()
    if choice == "c":
        return {"cancelled": True}
    saved = client.command(
        {
            "action": "save_draft",
            "module": module,
            "expected_revision": description["module"]["revision"],
            "config": candidate,
        }
    )
    if choice == "s":
        return saved
    validated = client.command(
        {
            "action": "validate",
            "module": module,
            "expected_revision": saved["module"]["revision"],
        }
    )
    activation: dict[str, Any] = {
        "action": "activate",
        "module": module,
        "expected_revision": validated["module"]["revision"],
        "validation_id": validated["validation"]["validation_id"],
        "idempotency_key": secrets.token_urlsafe(18),
    }
    if module == "core_admin":
        activation["config"] = {
            "password": admin_password
            or password
            or getpass.getpass("Administrator password: ")
        }
    return client.command(activation)


def _handle_configure(args: argparse.Namespace) -> None:
    client = _client(args)
    module = args.module or _choose_module(client)
    print_output(
        _configure_one(client, module),
        output=args.output,
    )


def _handle_init(args: argparse.Namespace) -> None:
    print_output(
        _configure_one(_client(args), "core_admin"),
        output=args.output,
    )


def _handle_state_action(args: argparse.Namespace) -> None:
    client = _client(args)
    description = client.command(
        {"action": "describe", "module": args.module}
    )
    body: dict[str, Any] = {
        "action": args.config_command,
        "module": args.module,
        "expected_revision": description["module"]["revision"],
    }
    if args.config_command == "disable":
        body["idempotency_key"] = secrets.token_urlsafe(18)
    print_output(client.command(body), output=args.output)


def _handle_apply(args: argparse.Namespace) -> None:
    declaration = yaml.safe_load(Path(args.file).read_text())
    if not isinstance(declaration, dict):
        raise CLIError("declaration must be a YAML object")
    print_output(
        apply_declaration(
            _client(args),
            declaration,
            dry_run=args.dry_run,
            stdin=sys.stdin,
        ),
        output=args.output,
    )


def _handle_export(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"action": "export"}
    if args.module:
        body["module"] = args.module
    result = _client(args).command(body)
    write_export(Path(args.file), result["export"])
    print(f"Wrote redacted configuration to {args.file}")


async def _issue_bootstrap_to_file(target: Path) -> None:
    if not target.is_absolute():
        raise CLIError("output path must be absolute")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        from server.configuration.bootstrap import (
            rotate_bootstrap_token,
        )
        from server.configuration.repository import ConfigRepository
        from server.db.engine import get_session_factory

        token = await rotate_bootstrap_token(
            ConfigRepository(get_session_factory())
        )
        os.write(descriptor, (token + "\n").encode("utf-8"))
    except Exception:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _handle_bootstrap_issue(args: argparse.Namespace) -> None:
    asyncio.run(_issue_bootstrap_to_file(Path(args.output)))
    print(f"Wrote one-time bootstrap token to {args.output}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (
        CLIError,
        DatabaseSchemaError,
        EnvironmentGenerationError,
        OSError,
        httpx.HTTPError,
    ) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
