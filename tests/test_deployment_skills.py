from __future__ import annotations

import ast
import base64
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
CANONICAL_ROOT = ROOT / ".agents" / "skills"
CLAUDE_ROOT = ROOT / ".claude" / "skills"
CURSOR_ROOT = ROOT / ".cursor" / "skills"
LEGACY_ROOT = ROOT / "docs" / "skills"
SKILL_NAMES = ("deploy-polardb-agentic-server",)
RETIRED_SKILL_NAMES = ("deploy-polardb-agentic-server-docker",)
LEGACY_RELEASE_UPLOAD_CONTRACT = {
    "version": "0.0.7",
    "revision": "9d358cd813cf07979a23cae7d80b3442231a1adb",
    "tools": {
        "prepare_document_upload",
        "resume_document_upload",
        "complete_document_upload",
        "abort_document_upload",
    },
    "complete_required": {"upload_session_id", "parts"},
}
CURRENT_ONBOARDING_MIN_VERSION = "0.0.8"
CURRENT_ONBOARDING_UPLOAD_CONTRACT = {
    "tools": {"prepare_document_upload", "complete_document_upload"},
    "complete_required": {"upload_session_id"},
}


def _upload_contract_for_release(revision: str) -> dict[str, set[str]]:
    source = subprocess.run(
        ["git", "show", f"{revision}:server/mcp/tools/polarrag.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = ast.parse(source)
    upload_tools: set[str] | None = None
    completion_arguments: set[str] | None = None
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "POLARRAG_UPLOAD_TOOL_NAMES" for target in node.targets
        ):
            assert isinstance(node.value, ast.Call)
            upload_tools = set(ast.literal_eval(node.value.args[0]))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "complete_document_upload":
            completion_arguments = {argument.arg for argument in node.args.args if argument.arg != "self"}
    assert upload_tools is not None
    assert completion_arguments is not None
    return {"tools": upload_tools, "complete_required": completion_arguments}


def _documented_upload_contract(text: str, revision: str) -> dict[str, set[str]]:
    for row in text.splitlines():
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if len(cells) != 4 or revision not in cells[0]:
            continue
        return {
            "tools": set(re.findall(r"`([^`]+_document_upload)`", cells[1])),
            "complete_required": set(re.findall(r"`(upload_session_id|parts)`", cells[2])),
        }
    raise AssertionError(f"missing upload compatibility row for {revision}")


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    raw = text.split("---\n", maxsplit=2)[1]
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", maxsplit=1)
        values[key] = value.strip().strip('"')
    return values


def _skill_path(root: Path, name: str) -> Path:
    return root / name / "SKILL.md"


def test_portable_skill_layout_is_discoverable_and_mirrored() -> None:
    for name in SKILL_NAMES:
        canonical = _skill_path(CANONICAL_ROOT, name)
        claude = _skill_path(CLAUDE_ROOT, name)
        assert canonical.is_file()
        assert claude.is_file()
        assert canonical.read_bytes() != claude.read_bytes()
        for path in (CANONICAL_ROOT / name).rglob("*"):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            relative = path.relative_to(CANONICAL_ROOT / name)
            expected = (CANONICAL_ROOT / name / relative).read_bytes()
            assert (CLAUDE_ROOT / name / relative).read_bytes() == expected
        assert not _skill_path(LEGACY_ROOT, name).exists()
        openai_config = yaml.safe_load((CANONICAL_ROOT / name / "agents" / "openai.yaml").read_text(encoding="utf-8"))
        assert openai_config["policy"]["allow_implicit_invocation"] is False
        assert openai_config["interface"]["display_name"]
        assert 25 <= len(openai_config["interface"]["short_description"]) <= 64
        assert f"${name}" in openai_config["interface"]["default_prompt"]

    for name in RETIRED_SKILL_NAMES:
        for root in (CANONICAL_ROOT, CLAUDE_ROOT, CURSOR_ROOT, LEGACY_ROOT):
            assert not (root / name).exists()
    assert not (CURSOR_ROOT / "deploy-polardb-agentic-server").exists()


def test_unified_skill_bundles_both_deployment_modes() -> None:
    skill_root = CANONICAL_ROOT / "deploy-polardb-agentic-server"
    text = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert (skill_root / "scripts" / "deploy-source.sh").is_file()
    assert (skill_root / "scripts" / "deploy-docker.sh").is_file()
    assert "bash scripts/deploy-source.sh --validate-only" in text
    assert "bash scripts/deploy-docker.sh --validate-only" in text


def test_skill_guides_optional_polarrag_mcp_delivery() -> None:
    path = _skill_path(CANONICAL_ROOT, "deploy-polardb-agentic-server")
    text = path.read_text(encoding="utf-8")

    assert 'version: "1.5"' in text
    required = {
        "PolarRAG MCP delivery",
        "docs/en/knowledge/polarrag-onboarding.md",
        "READY",
        "CURRENT",
        "polarrag",
        "PUBLIC",
        "PERSONAL",
        "pas_user_agent_",
        "prepare_document_upload",
        "complete_document_upload",
        "capability_missing",
    }
    assert not [term for term in required if term not in text]
    assert "Do not deploy or reconfigure PolarRAG" in text


def test_legacy_release_upload_contract_is_routed_away_from_current_onboarding() -> None:
    skill_root = CANONICAL_ROOT / "deploy-polardb-agentic-server"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    release_contract = _upload_contract_for_release(LEGACY_RELEASE_UPLOAD_CONTRACT["revision"])

    for script_name in ("deploy-source.sh", "deploy-docker.sh"):
        script = (skill_root / "scripts" / script_name).read_text(encoding="utf-8")
        assert f'PAS_VERSION="${{PAS_VERSION:-{CURRENT_VERSION}}}"' in script

    assert release_contract == {
        "tools": LEGACY_RELEASE_UPLOAD_CONTRACT["tools"],
        "complete_required": LEGACY_RELEASE_UPLOAD_CONTRACT["complete_required"],
    }
    legacy_guide = subprocess.run(
        [
            "git",
            "show",
            f"{LEGACY_RELEASE_UPLOAD_CONTRACT['revision']}:docs/en/knowledge/polarrag-mcp.md",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert legacy_guide
    assert LEGACY_RELEASE_UPLOAD_CONTRACT["revision"] in skill
    assert "Legacy v0.0.7 upload contract" in skill
    assert "Current onboarding upload contract" in skill
    assert "bundled release" in skill
    assert f"`v{CURRENT_ONBOARDING_MIN_VERSION}` or later `PAS_REF`" in skill
    for tool in LEGACY_RELEASE_UPLOAD_CONTRACT["tools"]:
        assert tool in skill
    for argument in LEGACY_RELEASE_UPLOAD_CONTRACT["complete_required"]:
        assert argument in skill
    for tool in CURRENT_ONBOARDING_UPLOAD_CONTRACT["tools"]:
        assert tool in skill
    assert "Only `upload_session_id` is accepted" in skill
    assert "$PAS_HOME/docs/en/knowledge/polarrag-onboarding.md" in skill
    for path in (
        "docs/en/knowledge/polarrag-onboarding.md",
        "docs/zh-cn/knowledge/polarrag-onboarding.md",
    ):
        guide = (ROOT / path).read_text(encoding="utf-8")
        assert _documented_upload_contract(guide, LEGACY_RELEASE_UPLOAD_CONTRACT["version"]) == release_contract
        assert _documented_upload_contract(guide, CURRENT_ONBOARDING_MIN_VERSION) == (
            CURRENT_ONBOARDING_UPLOAD_CONTRACT
        )


def test_skill_metadata_requires_explicit_safe_invocation() -> None:
    for name in SKILL_NAMES:
        path = _skill_path(CANONICAL_ROOT, name)
        metadata = _frontmatter(path)
        assert metadata["name"] == name
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        assert metadata["description"].startswith("Use when the user explicitly")
        assert "disable-model-invocation" not in metadata
        assert metadata["license"] == "Apache-2.0"
        assert "compatibility" not in metadata
        assert "Linux target compatibility" in path.read_text(encoding="utf-8")
        vendor_metadata = _frontmatter(_skill_path(CLAUDE_ROOT, name))
        assert vendor_metadata["disable-model-invocation"] == "true"


def test_skill_instructions_keep_secrets_out_of_agent_context() -> None:
    for name in SKILL_NAMES:
        text = _skill_path(CANONICAL_ROOT, name).read_text(encoding="utf-8")
        assert "--validate-only" in text
        assert "POLARDB_PASSWORD='<password>'" not in text
        assert "collect missing ones from the user" not in text
        assert "relay it including" not in text
        assert "Do not ask the user to send" in text
        assert "Do not print or relay" in text


def test_deployment_scripts_enforce_reviewed_safety_invariants() -> None:
    source = (CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / "deploy-source.sh").read_text(
        encoding="utf-8"
    )
    docker = (CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / "deploy-docker.sh").read_text(
        encoding="utf-8"
    )

    for text in (source, docker):
        assert "umask 077" in text
        assert "--validate-only" in text
        assert f'PAS_VERSION="${{PAS_VERSION:-{CURRENT_VERSION}}}"' in text
        assert 'PAS_REF="${PAS_REF:-v${PAS_VERSION}}"' in text
        assert 'fetch --depth 1 origin "$PAS_REF"' in text
        assert "checkout --detach FETCH_HEAD" in text
        assert "verify_checkout_identity" in text
        assert "repository has uncommitted changes" in text
        assert "export POLARDB_HOST POLARDB_PORT POLARDB_USER POLARDB_PASSWORD" not in text
        assert "POLARDB_PASSWORD='<password>'" not in text
        assert "Bootstrap token (valid 15 min): $BOOTSTRAP_TOKEN" not in text

    assert 'wait_http "http://127.0.0.1:$BACKEND_PORT/readyz"' in source
    assert '/health"' not in source
    assert "pkill -f" not in source
    assert "uv export --frozen" in source
    assert "uv pip sync" in source
    assert "--require-hashes" in source
    assert '"$PAS_HOME/.venv/bin/alembic" upgrade head' in source
    assert '"$PAS_HOME/.venv/bin/pas" config bootstrap-token issue' in source
    assert 'UV_INDEX_URL="$PYPI_INDEX" uv sync' not in source
    assert "set -a" not in source
    assert 'PAS_DATABASE_URL="$PAS_DATABASE_URL_VALUE"' in source

    assert '"${DOCKER_COMMAND[@]}" run --rm -i' in docker
    assert not re.search(r"unset\s+[^\n]*PAS_PORT", docker)
    assert "BOOTSTRAP_TOKEN=" not in docker
    assert 'grep "Bootstrap token:"' not in docker
    assert "compose()" in docker
    assert 'image inspect --format "{{.Architecture}}"' in docker
    assert "image architecture" in docker
    assert 'PAS_ALLOW_LOCAL_BUILD="${PAS_ALLOW_LOCAL_BUILD:-0}"' in docker
    assert 'if [ "$PAS_ALLOW_LOCAL_BUILD" = "1" ]; then' in docker
    assert docker.index('if [ "$PAS_ALLOW_LOCAL_BUILD" = "1" ]; then') < docker.index('image inspect "$PAS_IMAGE"')
    assert "image pull failed and local build is disabled" in docker
    assert 'PAS_DATABASE_ENGINE="${PAS_DATABASE_ENGINE:-mysql}"' in docker
    assert "initialize_sqlite_volume" in docker
    assert "PAS_SQLITE_VOLUME" in docker
    assert "type=volume,source=$PAS_SQLITE_VOLUME,target=/var/lib/pas" in docker


def test_docker_skill_documents_explicit_persistent_sqlite_mode() -> None:
    skill = (CANONICAL_ROOT / "deploy-polardb-agentic-server" / "SKILL.md").read_text(encoding="utf-8")

    assert "PAS_DATABASE_ENGINE=sqlite" in skill
    assert "Docker named volume" in skill
    assert "PAS_PORT" in skill
    assert "POLARDB_*" in skill


def test_sqlite_compose_uses_persistent_named_volume() -> None:
    compose = yaml.safe_load((ROOT / "deploy" / "compose" / "compose.sqlite.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"migrate", "server"}
    for name in ("migrate", "server"):
        assert services[name]["environment"] == {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:////var/lib/pas/pas.db",
            "PAS_ENCRYPTION_KEY": "file:/var/lib/pas/pas_encryption_key",
        }
        assert "pas-data:/var/lib/pas" in services[name]["volumes"]
        assert services[name]["read_only"] is True
    assert services["migrate"]["command"] == ["database", "migrate"]
    assert services["server"]["ports"] == ["${PAS_PORT:-18760}:18760"]
    assert compose["volumes"]["pas-data"]["name"] == "${PAS_SQLITE_VOLUME:?set PAS_SQLITE_VOLUME}"
    assert compose["volumes"]["pas-data"]["external"] is True


def test_skill_scripts_are_valid_bash() -> None:
    scripts = [
        CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / "deploy-source.sh",
        CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / "deploy-docker.sh",
    ]
    result = subprocess.run(
        ["bash", "-n", *(str(path) for path in scripts)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_checked_in_agent_mirrors_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/sync-agent-skills.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_public_release_exports_agent_skill_roots() -> None:
    allowlist = {
        line.strip()
        for line in (ROOT / ".public-release-allowlist").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".agents/" in allowlist
    assert ".claude/" in allowlist
    assert ".cursor/" not in allowlist
    assert "docs/skills/" not in allowlist


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _validation_path(tmp_path: Path, *, docker: bool) -> str:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    _write_executable(binary_dir / "uname", "#!/bin/sh\necho Linux\n")
    _write_executable(
        binary_dir / "id",
        '#!/bin/sh\ncase "${1:-}" in -u|-g) echo 1000 ;; *) echo test ;; esac\n',
    )
    _write_executable(binary_dir / "python3", "#!/bin/sh\nexit 0\n")
    if docker:
        _write_executable(binary_dir / "docker", "#!/bin/sh\nexit 0\n")
    return str(binary_dir)


def _run_validation(
    script_name: str,
    tmp_path: Path,
    *,
    pas_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    include_polardb_inputs: bool = True,
) -> subprocess.CompletedProcess[str]:
    docker = script_name == "deploy-docker.sh"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": _validation_path(tmp_path, docker=docker),
        "PAS_HOME": str(pas_home or (tmp_path / "pas")),
    }
    if include_polardb_inputs:
        env.update(
            {
                "POLARDB_HOST": "db.example.invalid",
                "POLARDB_USER": "pas_user",
            }
        )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "/bin/bash",
            str(CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / script_name),
            "--validate-only",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_docker_sqlite_validation(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_validation(
        "deploy-docker.sh",
        tmp_path,
        extra_env={"PAS_DATABASE_ENGINE": "sqlite", **(extra_env or {})},
        include_polardb_inputs=False,
    )


def test_validate_only_does_not_require_sudo_when_prerequisites_are_ready(
    tmp_path: Path,
) -> None:
    for script_name in ("deploy-source.sh", "deploy-docker.sh"):
        case_root = tmp_path / script_name
        case_root.mkdir()
        result = _run_validation(script_name, case_root)
        assert result.returncode == 0, result.stderr
        assert "validate-only completed" in result.stdout


def test_docker_validation_accepts_explicit_sqlite_without_mysql(
    tmp_path: Path,
) -> None:
    result = _run_docker_sqlite_validation(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "SQLite inputs" in result.stdout


@pytest.mark.parametrize("engine", ("postgres", "SQLite"))
def test_docker_validation_rejects_invalid_database_engine(
    engine: str,
    tmp_path: Path,
) -> None:
    result = _run_docker_sqlite_validation(
        tmp_path,
        {"PAS_DATABASE_ENGINE": engine},
    )

    assert result.returncode != 0
    assert "PAS_DATABASE_ENGINE" in result.stderr


def test_docker_validation_rejects_polardb_input_in_sqlite_mode(
    tmp_path: Path,
) -> None:
    result = _run_docker_sqlite_validation(
        tmp_path,
        {"POLARDB_HOST": "db.example.invalid"},
    )

    assert result.returncode != 0
    assert "POLARDB_HOST" in result.stderr


def test_docker_validation_keeps_mysql_required_by_default(tmp_path: Path) -> None:
    result = _run_validation(
        "deploy-docker.sh",
        tmp_path,
        include_polardb_inputs=False,
    )

    assert result.returncode != 0
    assert "POLARDB_HOST" in result.stderr


def _prepare_checkout(case_root: Path, origin: str) -> Path:
    git = shutil.which("git")
    assert git is not None
    checkout = case_root / "pas"
    checkout.mkdir()
    (checkout / "server").mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "alibabacloud-polardb-tool-agentic-server"\n',
        encoding="utf-8",
    )
    (checkout / "deploy" / "compose").mkdir(parents=True)
    (checkout / "deploy" / "compose" / "compose.external-mysql.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (checkout / "deploy" / "compose" / "compose.sqlite.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    subprocess.run([git, "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        [git, "remote", "add", "origin", origin],
        cwd=checkout,
        check=True,
    )
    subprocess.run([git, "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=checkout,
        check=True,
    )
    return checkout


def _add_real_git_to_validation_path(case_root: Path, *, docker: bool) -> None:
    git = shutil.which("git")
    assert git is not None
    binary_dir = Path(_validation_path(case_root, docker=docker))
    _write_executable(binary_dir / "git", f'#!/bin/sh\nexec "{git}" "$@"\n')
    _write_executable(binary_dir / "ls", '#!/bin/sh\nexec /bin/ls "$@"\n')


def _prepare_docker_deployment_with_fake_engine(
    tmp_path: Path,
    *,
    pas_home: Path,
    database_engine: str = "mysql",
    buildx_available: bool = True,
    docker_initially_available: bool = True,
) -> tuple[list[str], dict[str, str], Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(binary_dir / "uname", "#!/bin/sh\necho Linux\n")
    _write_executable(
        binary_dir / "id",
        '#!/bin/sh\ncase "${1:-}" in -u|-g) echo 1000 ;; *) echo test ;; esac\n',
    )
    _write_executable(binary_dir / "python3", "#!/bin/sh\necho READY\n")
    _write_executable(binary_dir / "curl", '#!/bin/sh\necho \'{"mode":"READY"}\'\n')
    _write_executable(binary_dir / "hostname", "#!/bin/sh\necho 127.0.0.1\n")
    fake_docker = (
        f"#!{sys.executable}\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(args) + '\\n')\n"
        "started_marker = os.environ.get('FAKE_DOCKER_STARTED_MARKER')\n"
        "if started_marker and (args == ['info'] or args == ['compose', 'version']):\n"
        "    if not Path(started_marker).is_file():\n"
        "        raise SystemExit(1)\n"
        "if args and args[0] == 'version':\n"
        "    print('amd64')\n"
        "elif len(args) >= 2 and args[0] == 'buildx' and args[1] == 'version':\n"
        "    raise SystemExit(0 if os.environ['FAKE_BUILDX_AVAILABLE'] == '1' else 1)\n"
        "elif len(args) >= 2 and args[0] == 'image' and args[1] == 'inspect':\n"
        "    print('amd64')\n"
        "elif args and args[0] == 'run' and os.environ.get('FAKE_SQLITE_VOLUME_DIR'):\n"
        "    volume_dir = Path(os.environ['FAKE_SQLITE_VOLUME_DIR'])\n"
        "    source = sys.stdin.read().replace('/var/lib/pas', str(volume_dir))\n"
        "    source = 'import os\\nos.fchown = lambda *args: None\\nos.chown = lambda *args: None\\n' + source\n"
        "    raise SystemExit(subprocess.run([sys.executable, '-c', source], check=False).returncode)\n"
    )
    docker_path = binary_dir / "docker"
    if docker_initially_available:
        _write_executable(docker_path, fake_docker)
    else:
        after_install = binary_dir / "docker-after-install"
        package_log = tmp_path / "package.log"
        started_marker = tmp_path / "docker-started"
        _write_executable(after_install, fake_docker)
        _write_executable(
            binary_dir / "dnf",
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$FAKE_PACKAGE_LOG"\n'
            '/bin/cp "$FAKE_DOCKER_AFTER_INSTALL" "$FAKE_DOCKER_TARGET"\n'
            '/bin/chmod 755 "$FAKE_DOCKER_TARGET"\n',
        )
        _write_executable(binary_dir / "sudo", '#!/bin/sh\nexec "$@"\n')
        _write_executable(
            binary_dir / "systemctl",
            '#!/bin/sh\n[ "$*" = "enable --now docker" ] || exit 1\n'
            '/usr/bin/touch "$FAKE_DOCKER_STARTED_MARKER"\n',
        )
    git = shutil.which("git")
    assert git is not None
    _write_executable(binary_dir / "git", f'#!/bin/sh\nexec "{git}" "$@"\n')
    env: dict[str, str] = {
        "HOME": str(tmp_path / "home"),
        "PATH": (
            str(binary_dir)
            if not docker_initially_available
            else f"{binary_dir}:/usr/bin:/bin"
        ),
        "PAS_HOME": str(pas_home),
        "PAS_UPDATE_REPO": "0",
        "PAS_REF": "immutable-current-ref",
        "PAS_ALLOW_LOCAL_BUILD": "1",
        "FAKE_BUILDX_AVAILABLE": "1" if buildx_available else "0",
        "FAKE_DOCKER_LOG": str(docker_log),
    }
    if not docker_initially_available:
        env.update(
            {
                "FAKE_DOCKER_AFTER_INSTALL": str(after_install),
                "FAKE_DOCKER_TARGET": str(docker_path),
                "FAKE_PACKAGE_LOG": str(package_log),
                "FAKE_DOCKER_STARTED_MARKER": str(started_marker),
            }
        )
    if database_engine == "mysql":
        env.update(
            {
                "POLARDB_HOST": "db.example.invalid",
                "POLARDB_USER": "pas_user",
                "POLARDB_PASSWORD": "fixture-password",
            }
        )
    else:
        sqlite_volume_dir = tmp_path / "sqlite-volume"
        sqlite_volume_dir.mkdir(exist_ok=True)
        env.update(
            {
                "PAS_DATABASE_ENGINE": database_engine,
                "PAS_PORT": "18782",
                "FAKE_SQLITE_VOLUME_DIR": str(sqlite_volume_dir),
            }
        )
    return (
        [
            "/bin/bash",
            str(CANONICAL_ROOT / "deploy-polardb-agentic-server" / "scripts" / "deploy-docker.sh"),
        ],
        env,
        docker_log,
    )


def _run_docker_deployment_with_fake_engine(
    tmp_path: Path,
    *,
    pas_home: Path,
    database_engine: str = "mysql",
) -> tuple[subprocess.CompletedProcess[str], str]:
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=pas_home,
        database_engine=database_engine,
    )
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, docker_log.read_text(encoding="utf-8")


def test_local_build_for_custom_ref_never_pulls_the_default_image(
    tmp_path: Path,
) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    checkout = _prepare_checkout(tmp_path, official)
    result, docker_log = _run_docker_deployment_with_fake_engine(tmp_path, pas_home=checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"building pas-local:{CURRENT_VERSION} from the verified checkout" in result.stdout
    assert "pull " not in docker_log
    assert "build " in docker_log
    assert (f"ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:{CURRENT_VERSION}") not in (
        checkout / ".secrets" / "pas-compose.env"
    ).read_text(encoding="utf-8")


def test_local_build_requires_buildx_before_checkout(tmp_path: Path) -> None:
    pas_home = tmp_path / "not-created"
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=pas_home,
        buildx_available=False,
    )

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PAS_ALLOW_LOCAL_BUILD=1 requires Docker Buildx" in result.stderr
    assert "install a trusted Docker Buildx plugin" in result.stderr
    assert "approved PAS_IMAGE with PAS_ALLOW_LOCAL_BUILD=0" in result.stderr
    assert not pas_home.exists()
    commands = docker_log.read_text(encoding="utf-8")
    assert "buildx version" in commands
    assert "build " not in commands
    assert "volume create" not in commands
    assert " run " not in f" {commands}"


def test_validate_only_local_build_requires_buildx_without_side_effects(tmp_path: Path) -> None:
    pas_home = tmp_path / "not-created"
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=pas_home,
        buildx_available=False,
    )

    result = subprocess.run(
        [*command, "--validate-only"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PAS_ALLOW_LOCAL_BUILD=1 requires Docker Buildx" in result.stderr
    assert not pas_home.exists()
    commands = docker_log.read_text(encoding="utf-8")
    assert "buildx version" in commands
    assert "build " not in commands
    assert "volume create" not in commands
    assert " run " not in f" {commands}"


def test_newly_installed_docker_requires_buildx_before_checkout(tmp_path: Path) -> None:
    pas_home = tmp_path / "not-created"
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=pas_home,
        buildx_available=False,
        docker_initially_available=False,
    )

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PAS_ALLOW_LOCAL_BUILD=1 requires Docker Buildx" in result.stderr
    assert "installing Docker from the configured operating-system repositories" in result.stdout
    assert (tmp_path / "package.log").read_text(encoding="utf-8") == "install -y docker docker-compose-plugin\n"
    assert (tmp_path / "docker-started").is_file()
    assert not pas_home.exists()
    commands = docker_log.read_text(encoding="utf-8")
    assert "buildx version" in commands
    assert commands.index("info") < commands.index("compose version") < commands.index("buildx version")
    assert "build " not in commands
    assert "volume create" not in commands
    assert "compose -p" not in commands


def test_prebuilt_image_path_does_not_require_buildx(tmp_path: Path) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    checkout = _prepare_checkout(tmp_path, official)
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=checkout,
        buildx_available=False,
    )
    env["PAS_ALLOW_LOCAL_BUILD"] = "0"

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = docker_log.read_text(encoding="utf-8")
    assert "buildx version" not in commands
    assert "image inspect" in commands
    assert "build " not in commands


def test_docker_sqlite_deployment_initializes_named_volume_without_mysql(
    tmp_path: Path,
) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    checkout = _prepare_checkout(tmp_path, official)
    result, docker_log = _run_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=checkout,
        database_engine="sqlite",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "initializing persistent SQLite volume polardb-agentic-sqlite-data" in result.stdout
    assert "volume create polardb-agentic-sqlite-data" in docker_log
    assert "--mount type=volume,source=polardb-agentic-sqlite-data,target=/var/lib/pas" in docker_log
    assert "-f deploy/compose/compose.sqlite.yaml config --quiet" in docker_log
    assert "-f deploy/compose/compose.sqlite.yaml up -d" in docker_log
    assert docker_log.index("volume create") < docker_log.index("compose -p")
    assert "POLARDB_" not in docker_log
    assert "fixture-password" not in result.stdout + result.stderr + docker_log
    assert not (checkout / ".secrets" / "pas-compose.env").exists()
    key = tmp_path / "sqlite-volume" / "pas_encryption_key"
    assert len(base64.b64decode(key.read_text(encoding="utf-8"))) == 32
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_docker_sqlite_deployment_never_overwrites_an_existing_volume_key(
    tmp_path: Path,
) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    checkout = _prepare_checkout(tmp_path, official)
    sqlite_volume_dir = tmp_path / "sqlite-volume"
    sqlite_volume_dir.mkdir()
    original_key = base64.b64encode(b"a" * 32).decode("ascii") + "\n"
    key = sqlite_volume_dir / "pas_encryption_key"
    key.write_text(original_key, encoding="utf-8")
    key.chmod(0o600)

    result, _ = _run_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=checkout,
        database_engine="sqlite",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert key.read_text(encoding="utf-8") == original_key


def test_docker_sqlite_deployment_waits_for_volume_initialization_lock(
    tmp_path: Path,
) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    checkout = _prepare_checkout(tmp_path, official)
    sqlite_volume_dir = tmp_path / "sqlite-volume"
    sqlite_volume_dir.mkdir()
    lock_path = sqlite_volume_dir / ".pas-initialize.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys; "
                "stream = pathlib.Path(sys.argv[1]).open('a+b'); "
                "fcntl.flock(stream, fcntl.LOCK_EX); "
                "print('locked', flush=True); sys.stdin.buffer.read()"
            ),
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdin is not None
    assert holder.stdout.readline().strip() == "locked"
    command, env, docker_log = _prepare_docker_deployment_with_fake_engine(
        tmp_path,
        pas_home=checkout,
        database_engine="sqlite",
    )
    deployment = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if docker_log.exists() and "--entrypoint python" in docker_log.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    else:
        deployment.kill()
        raise AssertionError("deployment did not reach the SQLite initializer")
    assert deployment.poll() is None

    holder.stdin.close()
    assert holder.wait(timeout=5) == 0
    stdout, stderr = deployment.communicate(timeout=5)

    assert deployment.returncode == 0, stdout + stderr
    assert (sqlite_volume_dir / "pas_encryption_key").is_file()


def test_existing_checkout_origin_must_match_when_updates_are_enabled(
    tmp_path: Path,
) -> None:
    for script_name in ("deploy-source.sh", "deploy-docker.sh"):
        case_root = tmp_path / script_name
        case_root.mkdir()
        checkout = _prepare_checkout(
            case_root,
            "https://example.invalid/not-pas.git",
        )
        _add_real_git_to_validation_path(
            case_root,
            docker=script_name == "deploy-docker.sh",
        )

        result = _run_validation(script_name, case_root, pas_home=checkout)

        assert result.returncode != 0
        assert "origin does not match PAS_REPO" in result.stderr


def test_generated_deployment_state_is_allowed_but_other_untracked_files_are_not(
    tmp_path: Path,
) -> None:
    official = "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git"
    for script_name in ("deploy-source.sh", "deploy-docker.sh"):
        case_root = tmp_path / script_name
        case_root.mkdir()
        checkout = _prepare_checkout(case_root, official)
        (checkout / ".secrets").mkdir()
        (checkout / ".secrets" / "pas_encryption_key").write_text(
            "not-a-real-key\n",
            encoding="utf-8",
        )
        (checkout / "run").mkdir()
        (checkout / "run" / "backend.out").write_text(
            "fixture\n",
            encoding="utf-8",
        )
        _add_real_git_to_validation_path(
            case_root,
            docker=script_name == "deploy-docker.sh",
        )

        allowed = _run_validation(script_name, case_root, pas_home=checkout)
        assert allowed.returncode == 0, allowed.stderr

        (checkout / "unexpected.py").write_text("raise SystemExit\n", encoding="utf-8")
        rejected = _run_validation(script_name, case_root, pas_home=checkout)
        assert rejected.returncode != 0
        assert "repository has uncommitted changes" in rejected.stderr
