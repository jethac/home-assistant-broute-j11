#!/usr/bin/env python3
"""Fail if the repository contains credentials or stable device identifiers.

The scan is deliberately conservative: it looks for the shapes of the values a
B-route deployment must never publish (32-character authentication IDs,
12-character passwords, 64-bit MAC addresses, link-local IPv6 addresses, USB
serial numbers) and allows only the sample values published in ROHM's public
BP35C0-J11 documentation, which the protocol golden vectors are taken from.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIPPED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
}

SCANNED_SUFFIXES = {
    ".cfg",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Sample values published in ROHM's BP35C0-J11 B-route application note
# (No. 63AN028E) and UART IF specification (No. 63TR008E). They describe no real
# installation and are the golden vectors the protocol tests assert against.
PUBLIC_DOCUMENTATION_SAMPLES = frozenset(
    {
        "00112233445566778899AABBCCDDEEFF",
        "0123456789AB",
        "0050C2FFFEDC2822",
        "0250C2FFFEDC2822",
        "fe80::250:c2ff:fedc:2822",
        "FE80000000000000",
    }
)

# Synthetic values invented for this repository's fixtures. They are documented
# in tests/fixtures/README.md and are not derived from any device.
SYNTHETIC_FIXTURE_VALUES = frozenset(
    {
        "0000000000000000000000000000ABCD",
        "SYNTHETICPW1",
        "AABBCCDDEEFF0011",
        "A8BBCCDDEEFF0011",
        "fe80::a8bb:ccdd:eeff:11",
    }
)

ALLOWED = PUBLIC_DOCUMENTATION_SAMPLES | SYNTHETIC_FIXTURE_VALUES

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("32-character B-route authentication ID", re.compile(r"\b[0-9A-F]{32}\b")),
    ("64-bit MAC address", re.compile(r"\b[0-9A-F]{16}\b")),
    (
        "link-local IPv6 address",
        re.compile(r"\bfe80::[0-9a-f:]{4,}\b", re.IGNORECASE),
    ),
    (
        "USB serial number in a by-id path",
        re.compile(r"/dev/serial/by-id/[\w.:-]*_(?!SYNTHETIC)\w{8,}-if"),
    ),
)


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if SKIPPED_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def main() -> int:
    """Scan the repository and report every unexpected sensitive-looking value."""
    findings: list[str] = []
    for path in sorted(_iter_files()):
        if path == Path(__file__).resolve():
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    if match.group(0) in ALLOWED:
                        continue
                    findings.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                        f"possible {label} ({len(match.group(0))} characters)"
                    )

    if findings:
        print("Secret scan failed:")
        for finding in findings:
            print(f"  {finding}")
        return 1

    print(f"Secret scan clean ({len(_iter_files())} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
