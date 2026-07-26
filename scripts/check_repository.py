#!/usr/bin/env python3
"""User-facing repository consistency checks.

This catches stale paths/claims that make OSS repository packages look unrepeatable.
It is intentionally lightweight and runs without PX4/Gazebo/AirSim.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "README.md",
    "README_CN.md",
    "LICENSE",
    "Dockerfile",
    "Dockerfile.gazebo",
    "compose.yml",
    "compose.build.yml",
    "compose.gazebo.yml",
    ".dockerignore",
    "requirements-mock.txt",
    ".env.example",
    "pyproject.toml",
    "SECURITY.md",
    ".github/workflows/ci.yml",
    "tests/test_mock_adapter.py",
    "tests/test_server_smoke.py",
    "scripts/smoke_mock.sh",
    "scripts/smoke_mock.ps1",
    "scripts/doctor_gazebo.sh",
    "scripts/sim_quickstart.sh",
    "scripts/docker/start_gazebo_demo.sh",
    ".gitattributes",
]

STALE_PATTERNS = [
    r"clients/",
    r"requirements-edge\.txt",
    r"core\.doctor",
    r"/api/doctor/run",
    r"scripts/start_gz_sim\.sh",
    r"adapters/base_adapter\.py",
    r"adapters/adapter_factory\.py",
    r"skills/hard_skills\.py",
    r"`SOUL\.md`",
    r"`BODY\.md`",
    r"`MEMORY\.md`",
    r"`SKILLS\.md`",
    r"`WORLD_MAP\.md`",
    r"MEMORY\.md / SKILLS\.md",
    r"robot_profile/MEMORY\.md",
    r"robot_profile/SKILLS\.md",
    r"Multi-platform clients",
    r"15 React components",
]

TEXT_GLOBS = [
    "README*.md",
    "SECURITY.md",
    "scripts/*.sh",
    ".github/workflows/*.yml",
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).exists()]
    if missing:
        fail("missing required repository files: " + ", ".join(missing))

    offenders: list[str] = []
    combined = re.compile("|".join(STALE_PATTERNS))
    for glob in TEXT_GLOBS:
        for path in ROOT.glob(glob):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), 1):
                if combined.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    if offenders:
        fail("stale repository references found:\n" + "\n".join(offenders))

    for path in ROOT.glob("scripts/**/*.sh"):
        data = path.read_bytes()
        if b"\r\n" in data:
            fail(f"shell script must use LF line endings, not CRLF: {path.relative_to(ROOT)}")

    absolute_duplicates = list((ROOT / "Users").glob("**/*")) if (ROOT / "Users").exists() else []
    if absolute_duplicates:
        fail("repository contains accidental absolute-path duplicate tree under Users/")

    print("repository consistency checks passed")


if __name__ == "__main__":
    main()
