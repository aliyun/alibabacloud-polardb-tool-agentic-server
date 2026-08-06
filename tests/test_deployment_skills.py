from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
CANONICAL_ROOT = ROOT / ".agents" / "skills"
CLAUDE_ROOT = ROOT / ".claude" / "skills"
CURSOR_ROOT = ROOT / ".cursor" / "skills"
LEGACY_ROOT = ROOT / "docs" / "skills"
SKILL_NAMES = (
    "deploy-polardb-agentic-server",
)
RETIRED_SKILL_NAMES = ("deploy-polardb-agentic-server-docker",)


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
        openai_config = yaml.safe_load((
            CANONICAL_ROOT / name / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8"))
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
    source = (
        CANONICAL_ROOT
        / "deploy-polardb-agentic-server"
        / "scripts"
        / "deploy-source.sh"
    ).read_text(encoding="utf-8")
    docker = (
        CANONICAL_ROOT
        / "deploy-polardb-agentic-server"
        / "scripts"
        / "deploy-docker.sh"
    ).read_text(encoding="utf-8")

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
    assert "/health\"" not in source
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
    assert 'if [ "$PAS_ALLOW_LOCAL_BUILD" != "1" ]; then' in docker
    assert "image pull failed and local build fallback is disabled" in docker


def test_skill_scripts_are_valid_bash() -> None:
    scripts = [
        CANONICAL_ROOT
        / "deploy-polardb-agentic-server"
        / "scripts"
        / "deploy-source.sh",
        CANONICAL_ROOT
        / "deploy-polardb-agentic-server"
        / "scripts"
        / "deploy-docker.sh",
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
        for line in (ROOT / ".public-release-allowlist")
        .read_text(encoding="utf-8")
        .splitlines()
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
        "#!/bin/sh\n"
        "case \"${1:-}\" in -u|-g) echo 1000 ;; *) echo test ;; esac\n",
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
) -> subprocess.CompletedProcess[str]:
    docker = script_name == "deploy-docker.sh"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": _validation_path(tmp_path, docker=docker),
        "POLARDB_HOST": "db.example.invalid",
        "POLARDB_USER": "pas_user",
        "PAS_HOME": str(pas_home or (tmp_path / "pas")),
    }
    return subprocess.run(
        [
            "/bin/bash",
            str(
                CANONICAL_ROOT
                / "deploy-polardb-agentic-server"
                / "scripts"
                / script_name
            ),
            "--validate-only",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
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
    _write_executable(binary_dir / "ls", "#!/bin/sh\nexec /bin/ls \"$@\"\n")


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
    official = (
        "https://github.com/aliyun/"
        "alibabacloud-polardb-tool-agentic-server.git"
    )
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
