#!/usr/bin/env python3
"""Keep agent-specific skill mirrors in sync with canonical Agent Skills."""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = ROOT / ".agents" / "skills"
VENDOR_ROOTS = (
    ROOT / ".claude" / "skills",
)
SKILLS = (
    "deploy-polardb-agentic-server",
)
RETIRED_SKILLS = ("deploy-polardb-agentic-server-docker",)
RETIRED_MIRRORS = (
    ROOT / ".cursor" / "skills" / "deploy-polardb-agentic-server",
    ROOT / ".cursor" / "skills" / "deploy-polardb-agentic-server-docker",
)


def _files(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file()
    }


def _vendor_skill(content: bytes) -> bytes:
    marker = b"metadata:\n"
    if marker not in content:
        raise ValueError("SKILL.md frontmatter is missing metadata")
    return content.replace(
        marker,
        b"disable-model-invocation: true\n" + marker,
        1,
    )


def _same_tree(source: Path, target: Path) -> bool:
    source_files = _files(source)
    target_files = _files(target) if target.is_dir() else {}
    if source_files.keys() != target_files.keys():
        return False
    for relative, path in source_files.items():
        target_path = target_files[relative]
        expected = (
            _vendor_skill(path.read_bytes())
            if relative == Path("SKILL.md")
            else path.read_bytes()
        )
        if target_path.read_bytes() != expected:
            return False
        if stat.S_IMODE(path.stat().st_mode) != stat.S_IMODE(target_path.stat().st_mode):
            return False
    return True


def check() -> int:
    stale = [
        f"{path.relative_to(ROOT)} (retired)"
        for path in RETIRED_MIRRORS
        if path.exists()
    ]
    for vendor_root in VENDOR_ROOTS:
        for skill in RETIRED_SKILLS:
            if (vendor_root / skill).exists():
                stale.append(f"{vendor_root.parent.name}/{skill} (retired)")
        for skill in SKILLS:
            source = CANONICAL_ROOT / skill
            target = vendor_root / skill
            if not source.is_dir():
                print(f"missing canonical skill: {source}", file=sys.stderr)
                return 1
            if not _same_tree(source, target):
                stale.append(f"{vendor_root.parent.name}/{skill}")
    if stale:
        print(
            "Agent skill mirror is stale: "
            + ", ".join(stale)
            + ". Run scripts/sync-agent-skills.py --write.",
            file=sys.stderr,
        )
        return 1
    return 0


def write() -> int:
    for target in RETIRED_MIRRORS:
        if target.exists():
            shutil.rmtree(target)
    for vendor_root in VENDOR_ROOTS:
        vendor_root.mkdir(parents=True, exist_ok=True)
        for skill in RETIRED_SKILLS:
            target = vendor_root / skill
            if target.exists():
                shutil.rmtree(target)
        for skill in SKILLS:
            source = CANONICAL_ROOT / skill
            target = vendor_root / skill
            if not source.is_dir():
                print(f"missing canonical skill: {source}", file=sys.stderr)
                return 1
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, copy_function=shutil.copy2)
            skill_file = target / "SKILL.md"
            skill_file.write_bytes(_vendor_skill(skill_file.read_bytes()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    return check() if args.check else write()


if __name__ == "__main__":
    raise SystemExit(main())
