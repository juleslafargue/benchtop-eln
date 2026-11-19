"""0.36.4: `./benchtop update` stopped on a Mac with "NEWV?: unbound variable" (in every zip copy since 0.19.0).

macOS's bash 3.2, in a UTF-8 locale, reads the first byte of a non-ASCII character straight after a variable name
("$NEWV…") as part of the name, and `set -u` makes the unknown variable fatal. Bash 5 on Linux (and in CI) doesn't,
so nothing caught it. Every variable followed directly by a non-ASCII character must be written in braces: "${NEWV}…".
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UNBRACED = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*[\x80-\xff]")


def _shell_scripts() -> list[Path]:
    try:
        names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
        files = [REPO / n for n in names]
    except (OSError, subprocess.CalledProcessError):  # a copy without Git: look at the files themselves
        files = [p for p in REPO.rglob("*") if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts]
    out = []
    for p in files:
        if not p.is_file() or "node_modules" in p.parts:
            continue
        if p.suffix in (".sh", ".command", ".bash"):
            out.append(p)
            continue
        try:
            with open(p, "rb") as fh:
                first = fh.readline(200)
        except OSError:
            continue
        if re.match(rb"#!.*\b(ba|z|da|k)?sh\b", first):
            out.append(p)
    return out


def test_the_launchers_are_found():
    names = {p.relative_to(REPO).as_posix() for p in _shell_scripts()}
    assert {"benchtop", "Benchtop.command", "deploy/update-server.sh"} <= names, names


def test_no_variable_runs_into_a_non_ascii_character():
    found = []
    for p in _shell_scripts():
        for i, line in enumerate(p.read_bytes().splitlines(), 1):
            if UNBRACED.search(line):
                found.append(f"{p.relative_to(REPO)}:{i}: {line.decode('utf-8', 'replace').strip()}")
    assert not found, "write these in braces, like ${NEWV}… (macOS bash 3.2 reads the next byte as part of the name):\n" + "\n".join(found)


def test_the_release_page_tells_mac_zip_copies_how_to_get_past_it(tmp_path):
    sys.path.insert(0, str(REPO / "deploy"))
    import release
    src = tmp_path / "repo"
    src.mkdir()
    (src / "backend/app").mkdir(parents=True)
    (src / "backend/app/__init__.py").write_text('__version__ = "50.0.0"\n')
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "v50.0.0: first"]):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)
    n = release.notes(src, "50.0.0")
    footer = n.split("\n---\n", 1)[1]
    assert "unbound variable" in footer and "./benchtop upgrade ~/Downloads/benchtop-eln-v50.0.0.zip" in footer
