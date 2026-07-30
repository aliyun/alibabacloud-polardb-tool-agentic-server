from __future__ import annotations

import errno
import os
import pty
import stat
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/deploy/create-external-mysql-env.sh"
PROJECT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
DEFAULT_IMAGE = (
    "ghcr.io/aliyun/"
    f"alibabacloud-polardb-tool-agentic-server:{PROJECT_VERSION}"
)


def _install_fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable = bin_directory / "docker"
    arguments = tmp_path / "docker-arguments"
    executable.write_text(
        f"""#!{sys.executable}
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

arguments = sys.argv[1:]
Path(os.environ["FAKE_DOCKER_ARGUMENTS"]).write_text(
    "\\n".join(arguments) + "\\n",
    encoding="utf-8",
)
if os.environ.get("FAKE_DOCKER_FAIL"):
    print("fake Docker failure", file=sys.stderr)
    raise SystemExit(23)

mount = arguments[arguments.index("--mount") + 1]
fields = dict(field.split("=", 1) for field in mount.split(","))
generated = Path(fields["src"]) / "generated.env"
if not os.environ.get("FAKE_DOCKER_NO_FILE"):
    content = (
        "PAS_DATABASE_URL="
        "'mysql+asyncmy://pas:encoded%24value@db.example.test:3306/pas'\\n"
        "PAS_ENCRYPTION_KEY='test-generated-key'\\n"
    )
    command = arguments[arguments.index("database"):]
    if "--image" in command:
        image = command[command.index("--image") + 1]
        content += f"PAS_IMAGE='{{image}}'\\n"
    generated.write_text(content, encoding="utf-8")
    generated.chmod(stat.S_IRUSR | stat.S_IWUSR)

race_target = os.environ.get("FAKE_DOCKER_RACE_TARGET")
if race_target:
    Path(race_target).write_text("raced target\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_directory, arguments


def _run_launcher(
    tmp_path: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], str, Path]:
    bin_directory, recorded_arguments = _install_fake_docker(tmp_path)
    environment = {
        "PATH": (
            f"{bin_directory}{os.pathsep}"
            "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "FAKE_DOCKER_ARGUMENTS": str(recorded_arguments),
        "PAS_IMAGE": "inherited-image-must-not-be-used",
        "PAS_DATABASE_URL": "inherited-database-url-must-not-be-used",
        "PAS_ENCRYPTION_KEY": "inherited-key-must-not-be-used",
    }
    if extra_environment:
        environment.update(extra_environment)

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(LAUNCHER), *arguments],
        cwd=tmp_path,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    chunks: list[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(master)
    returncode = process.wait(timeout=10)
    completed = subprocess.CompletedProcess(
        process.args,
        returncode,
        b"",
        b"".join(chunks),
    )
    return completed, completed.stderr.decode(errors="replace"), recorded_arguments


def _recorded(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _temporary_directories(path: Path) -> list[Path]:
    return list(path.glob(".pas-env.*"))


def test_launcher_generates_default_environment_with_hardened_container(
    tmp_path: Path,
) -> None:
    result, output, arguments_path = _run_launcher(tmp_path)

    assert result.returncode == 0, output
    generated = tmp_path / ".env"
    assert generated.is_file()
    assert stat.S_IMODE(generated.stat().st_mode) == 0o600
    content = generated.read_text(encoding="utf-8")
    assert "PAS_DATABASE_URL=" in content
    assert "PAS_ENCRYPTION_KEY=" in content
    assert "PAS_IMAGE=" not in content
    assert not _temporary_directories(tmp_path)

    arguments = _recorded(arguments_path)
    assert arguments[:6] == [
        "run",
        "--rm",
        "--interactive",
        "--tty",
        "--pull=missing",
        "--read-only",
    ]
    assert arguments[6:10:2] == ["--tmpfs", "--user"]
    assert arguments[7] == "/tmp:rw,noexec,nosuid,nodev"
    assert arguments[9] == f"{os.getuid()}:{os.getgid()}"
    assert arguments[10] == "--mount"
    assert arguments[11].startswith("type=bind,src=")
    assert arguments[11].endswith(",dst=/output")
    assert arguments[12:] == [
        "--entrypoint",
        "pas",
        DEFAULT_IMAGE,
        "database",
        "create-env",
        "--output",
        "/output/generated.env",
    ]
    combined = "\n".join(arguments) + output
    assert "inherited-image-must-not-be-used" not in combined
    assert "inherited-database-url-must-not-be-used" not in combined
    assert "inherited-key-must-not-be-used" not in combined
    assert "-e" not in arguments
    assert "--env" not in arguments


def test_launcher_persists_only_an_explicit_image(
    tmp_path: Path,
) -> None:
    selected_image = "registry.example/pas:test"
    output_path = tmp_path / "selected.env"

    result, output, arguments_path = _run_launcher(
        tmp_path,
        "--output",
        str(output_path),
        "--image",
        selected_image,
        "--skip-connection-test",
    )

    assert result.returncode == 0, output
    assert (
        f"PAS_IMAGE='{selected_image}'"
        in output_path.read_text(encoding="utf-8")
    )
    arguments = _recorded(arguments_path)
    assert arguments[-8:] == [
        selected_image,
        "database",
        "create-env",
        "--output",
        "/output/generated.env",
        "--image",
        selected_image,
        "--skip-connection-test",
    ]


def test_launcher_refuses_to_overwrite_before_running_docker(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / ".env"
    output_path.write_text("keep\n", encoding="utf-8")

    result, output, arguments_path = _run_launcher(tmp_path)

    assert result.returncode != 0
    assert output_path.read_text(encoding="utf-8") == "keep\n"
    assert not arguments_path.exists()
    assert not _temporary_directories(tmp_path)
    assert "already exists" in output


def test_launcher_cleans_up_when_docker_fails(tmp_path: Path) -> None:
    result, output, _arguments_path = _run_launcher(
        tmp_path,
        extra_environment={"FAKE_DOCKER_FAIL": "1"},
    )

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert not _temporary_directories(tmp_path)
    assert "fake Docker failure" in output


def test_launcher_does_not_overwrite_a_concurrent_target(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / ".env"
    result, output, _arguments_path = _run_launcher(
        tmp_path,
        extra_environment={"FAKE_DOCKER_RACE_TARGET": str(output_path)},
    )

    assert result.returncode != 0
    assert output_path.read_text(encoding="utf-8") == "raced target\n"
    assert not _temporary_directories(tmp_path)
    assert "must-not-be-used" not in output


def test_launcher_cleans_up_when_container_omits_output(
    tmp_path: Path,
) -> None:
    result, _output, _arguments_path = _run_launcher(
        tmp_path,
        extra_environment={"FAKE_DOCKER_NO_FILE": "1"},
    )

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert not _temporary_directories(tmp_path)
