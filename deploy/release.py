#!/usr/bin/env python3
"""Build a Benchtop release: the zip people download, and its release notes.

GitHub runs this after the tests pass on main, whenever the version number has changed (the "release" job in
deploy/github-tests.yml), and publishes the result on the repository's Releases page. It works anywhere, too:

    python3 deploy/release.py --out DIR      (needs frontend/dist: run `npm run build` in frontend/ first)

The zip holds this commit's files, as `git archive` gives them, plus the ready-built web interface
(frontend/dist), so running it needs no Node. Everything sits in one benchtop/ folder, which is what
./benchtop upgrade and ./benchtop update expect (including the launchers of older versions).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAILERS = re.compile(r"^(Co-Authored-By|Claude-Session|Signed-off-by|Reviewed-by|Change-Id):", re.I)
# GitHub's own settings (updates never touch them), the project website, and the upgrade test's old backups (tests only)
LEAVE_OUT = (".github/", "site/", "backend/tests/upgrades/")


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def version(root: Path = ROOT) -> str:
    init = (root / "backend/app/__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"', init, re.M)
    if not m:
        raise SystemExit("backend/app/__init__.py has no __version__")
    v = m.group(1)
    pkg = json.loads((root / "frontend/package.json").read_text()).get("version")
    if pkg != v:
        raise SystemExit(f"Versions disagree: backend/app/__init__.py says {v}, frontend/package.json says {pkg}. "
                         "Bump both (the release routine), then try again.")
    return v


def _commits(root: Path, v: str) -> list[str]:
    """The commits this release adds: everything since the previous release's tag. Before the first release
    there is no tag, so it's just the commit that set this version."""
    try:
        prev = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD^", root=root).strip()
    except subprocess.CalledProcessError:
        prev = ""
    if prev:
        return git("log", "--no-merges", "--format=%H", f"{prev}..HEAD", root=root).split()
    for h in git("log", "--no-merges", "--format=%H", "-n", "200", root=root).split():
        if git("log", "-1", "--format=%s", h, root=root).startswith(f"v{v}"):
            return [h]
    return git("log", "--no-merges", "--format=%H", "-n", "1", root=root).split()


def no_mentions(text: str) -> str:
    """On a GitHub release page, "@name" links (and tells) the GitHub account of that name, a stranger. Commit messages
    write "`@mentions`" for the feature or "`@nn`" for a typed example; put any bare @word in code so it stays text.
    An email address (letters right before the @) and text already in backticks are left alone."""
    out = []
    for i, chunk in enumerate(re.split(r"(`[^`\n]*`)", text)):
        out.append(chunk if i % 2 else re.sub(r"(?<![\w`])@([A-Za-z0-9][\w.-]*[\w]|[A-Za-z0-9])", r"`@\1`", chunk))
    return "".join(out)


def notes(root: Path = ROOT, v: str | None = None) -> str:
    v = v or version(root)
    parts = []
    for h in _commits(root, v):
        subject = no_mentions(git("log", "-1", "--format=%s", h, root=root).strip())
        body = [no_mentions(ln) for ln in git("log", "-1", "--format=%b", h, root=root).splitlines() if not TRAILERS.match(ln.strip())]
        title = re.sub(r"^v\d+(\.\d+)*:\s*", "", subject)
        title = title[:1].upper() + title[1:]
        text = "\n".join(body).strip()
        parts.append(f"### {title}\n\n{text}" if text else f"### {title}")
    how = (f"**To update:** close Benchtop, then in Terminal, in the Benchtop folder, run `./benchtop update` "
           f"(it shows what's new, backs up your data first, and `./benchtop rollback` undoes it). "
           f"Versions before 0.19 don't have that command yet: download **benchtop-eln-v{v}.zip** below and run "
           f"`./benchtop upgrade ~/Downloads/benchtop-eln-v{v}.zip` once. "
           f"A lab server updates with `deploy/update-server.sh` (docs/SERVER.md).\n\n"
           f"**New install:** download **benchtop-eln-v{v}.zip** below, unzip it, and double-click Benchtop.command "
           f"(README: Quick start (Mac)). On Linux, or on Windows through WSL, follow the README's Quick start (Linux) "
           f"or On Windows. It includes the ready-built interface, so you only need Python.")
    # "---" separates what changed from how to install it; ./benchtop update shows only the first part
    return "\n\n".join(parts) + "\n\n---\n\n" + how + "\n"


def build_zip(root: Path, v: str, out: Path) -> Path:
    dist = root / "frontend/dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("frontend/dist isn't built. Run `npm run build` in frontend/ first.")
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"benchtop-eln-v{v}.zip"
    tar = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=root, check=True, capture_output=True).stdout
    names: set[str] = set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z, \
            tarfile.open(fileobj=io.BytesIO(tar)) as t:
        for m in t.getmembers():
            if not m.isfile() or m.name.startswith(LEAVE_OUT) or m.name.startswith("frontend/dist/"):
                continue
            zi = zipfile.ZipInfo(f"benchtop/{m.name}", time.localtime(m.mtime)[:6])
            zi.external_attr = (0o100000 | (m.mode & 0o777)) << 16  # keeps ./benchtop and Benchtop.command runnable
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, t.extractfile(m).read())  # type: ignore[union-attr]
            names.add(m.name)
        # built after the sources, and dated now, so ./benchtop sees the interface is up to date and doesn't rebuild it
        now = time.localtime()[:6]
        for f in sorted(dist.rglob("*")):
            if f.is_file():
                zi = zipfile.ZipInfo(f"benchtop/frontend/dist/{f.relative_to(dist).as_posix()}", now)
                zi.external_attr = 0o100644 << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, f.read_bytes())
    for must in ("benchtop", "Benchtop.command", "backend/app/__init__.py", "backend/requirements.txt"):
        if must not in names:
            raise SystemExit(f"The zip would be missing {must}")
    return target


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="folder to write the zip and notes.md into")
    a = ap.parse_args(argv)
    v = version()
    z = build_zip(ROOT, v, a.out)
    (a.out / "notes.md").write_text(notes(ROOT, v))
    print(f"Benchtop {v}: {z} ({z.stat().st_size / 1e6:.1f} MB) and {a.out / 'notes.md'}")


if __name__ == "__main__":
    main(sys.argv[1:])
