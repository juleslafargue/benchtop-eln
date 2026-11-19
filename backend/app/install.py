"""Swap one version of Benchtop's program files for another, for ./benchtop update, upgrade and rollback.

    python -m app.install mirror NEW ROOT [--expect-version V]   put the version unpacked in NEW into ROOT
    python -m app.install snapshot ROOT DEST                     copy ROOT's program files into DEST (for rollback)

Your data is never touched: the data and backup folders, settings, the Python environment, node_modules and Git's
files are always left alone (wherever BENCHTOP_DATA_DIR and BENCHTOP_BACKUP_DIR point, too; a relative path there
means inside backend/, as it does for Benchtop itself).

Which files are Benchtop's (0.36.3): a release zip lists its own files in .benchtop-files. An update removes only
files the installed version listed there and the new version no longer has; anything else in Benchtop's folders
(a copy of your data folder, a protocol you saved in docs/) is yours and stays where it is. A copy installed before
that list existed can't tell its old files from yours, so those are moved, not deleted, into a folder named
Benchtop-kept-files-<date> next to the launcher. A file of yours in the way of one of Benchtop's is moved there too.
Nothing is ever deleted from a folder that holds a database (benchtop.db).

The new version is checked first: if it isn't a whole Benchtop (a copy left half-made or half-removed), nothing is
changed. Each file is written beside the old one and then renamed into place, so even the launcher that's running
this is replaced cleanly. Standard library only, on purpose: rollback runs this file on its own.
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

KEEP = (".git", ".github", ".benchtop-previous", ".benchtop-previous.new", ".benchtop-previous.old", "backend/data",
        "backend/.venv", "frontend/node_modules", "deploy/.env", "deploy/backups", "deploy/backup-copy")
PROGRAM_DIRS = ("backend", "frontend", "docs", "deploy", "plugins", "samples")
SKIP_NAMES = ("__pycache__", ".DS_Store", ".pytest_cache")
MANIFEST = ".benchtop-files"  # the release's own list of its files (deploy/release.py writes it into the zip)
CORE = ("benchtop", "backend/app/__init__.py", "backend/app/main.py", "backend/requirements.txt")
KEPT_FOLDER = "Benchtop-kept-files-"
DATA_VARS = ("BENCHTOP_DATA_DIR", "BENCHTOP_BACKUP_DIR", "BENCHTOP_OBSIDIAN_DIR", "BENCHTOP_BACKUP_COPY_DIR")


class Refused(Exception):
    """The new version isn't fit to install; nothing was changed."""


def _junk(name: str) -> bool:
    """Files Python, the build or the Finder make by themselves; removing them loses nothing."""
    return name in SKIP_NAMES or name.endswith((".pyc", ".tsbuildinfo"))


def _protected(root: Path) -> list[Path]:
    """Folders that hold data, wherever they are set to be (they may be inside the Benchtop folder). A relative path
    is relative to backend/, where Benchtop runs, whichever folder this copier was started from."""
    out = []
    for var in DATA_VARS:
        v = os.environ.get(var)
        if not v:
            continue
        for p in {Path(v), Path(v).expanduser()}:  # the data folder setting doesn't expand ~, the others do: keep both
            out.append((p if p.is_absolute() else root / "backend" / p).resolve())
    return out + [(root / k).resolve() for k in KEEP]


def read_manifest(folder: Path) -> Optional[set[str]]:
    """The files a release says are its own, or None for a copy from before releases listed them."""
    try:
        text = (folder / MANIFEST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    names = {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}
    return {n for n in names if not n.startswith("/") and ".." not in n.split("/")}


def write_manifest(folder: Path, names: set[str], version: str = "") -> None:
    head = (f"# The files of Benchtop {version}".rstrip() + ". Updates replace or remove only these; anything else in "
            "Benchtop's folders is yours and is left alone.\n")
    (folder / MANIFEST).write_text(head + "".join(f"{n}\n" for n in sorted(names)), encoding="utf-8")


def version_in(folder: Path) -> str:
    try:
        m = re.search(r'^__version__ = "([^"]+)"', (folder / "backend/app/__init__.py").read_text(), re.M)
    except OSError:
        return ""
    return m.group(1) if m else ""


def problem_with(new: Path, expect_version: Optional[str] = None) -> Optional[str]:
    """Why NEW isn't a whole Benchtop to install, or None."""
    for f in CORE:
        if not (new / f).is_file():
            return f"{new} isn't a whole copy of Benchtop: {f} is missing"
    listed = read_manifest(new)
    if listed:
        missing = sorted(n for n in listed if not os.path.lexists(new / n))
        if missing:
            return f"{new} isn't a whole copy of Benchtop: {len(missing)} of its files are missing (e.g. {missing[0]})"
    if expect_version and version_in(new) != expect_version:
        return f"{new} is Benchtop {version_in(new) or '(no version)'}, not {expect_version}"
    return None


def _has_data(p: Path) -> bool:
    if p.is_symlink() or not p.is_dir():
        return p.name == "benchtop.db"
    return any(True for _ in p.rglob("benchtop.db"))


class _Rules:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.protected = _protected(self.root)

    def kept(self, rel: str) -> bool:
        if any(rel == k or rel.startswith(k + "/") for k in KEEP):
            return True
        p = (self.root / rel).resolve()
        return any(p == q or q in p.parents for q in self.protected)

    def contains_protected(self, rel: str) -> bool:
        p = (self.root / rel).resolve()
        return any(p in q.parents for q in self.protected)


class _Aside:
    """Moves things out of the way into one Benchtop-kept-files-<date> folder (made only if needed)."""

    def __init__(self, root: Path):
        self.root = root
        self.folder: Optional[Path] = None
        self.moved: list[str] = []

    def move(self, rel: str) -> None:
        if self.folder is None:
            base = self.root / (KEPT_FOLDER + time.strftime("%Y-%m-%d"))
            folder, n = base, 1
            while os.path.lexists(folder):
                n += 1
                folder = base.with_name(f"{base.name}-{n}")
            self.folder = folder
        dest = self.folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / rel), str(dest))
        self.moved.append(rel)


def _place(src: Path, dst: Path) -> bool:
    """Copy one file into place (atomically). Returns True if it changed anything."""
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir():
        raise IsADirectoryError(f"{dst} is a folder")  # mirror() moves a folder in the way aside first
    elif dst.is_file() and not src.is_symlink() and filecmp.cmp(src, dst, shallow=False):
        shutil.copystat(src, dst)  # same content; keep the release's dates so "is the interface built?" stays right
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".benchtop-new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    if src.is_symlink():
        os.symlink(os.readlink(src), tmp)
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def mirror(new: Path, root: Path, expect_version: Optional[str] = None) -> dict:
    new, root = new.resolve(), root.resolve()
    problem = problem_with(new, expect_version)
    if problem:
        raise Refused(problem)
    rules = _Rules(root)
    shipped = read_manifest(root)  # what the installed version says is its own (None: an older copy, can't tell)

    def ours(rel: str) -> bool:
        return shipped is not None and (rel in shipped or rel == MANIFEST)

    aside = _Aside(root)
    changed = removed = 0
    emptied: set[Path] = set()  # folders something was taken out of: removed at the end if empty and not needed

    def drop(rel: str, count: bool = True) -> None:
        nonlocal removed
        (root / rel).unlink()
        removed += count
        emptied.add((root / rel).parent)

    def move_aside(rel: str) -> None:
        aside.move(rel)
        emptied.add((root / rel).parent)

    new_files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(new):
        rel_dir = os.path.relpath(dirpath, new)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES and not rules.kept(os.path.join(rel_dir, d))]
        for f in filenames:
            rel = os.path.join(rel_dir, f)
            if f in SKIP_NAMES or rules.kept(rel):
                continue
            new_files.add(rel)
            target = root / rel
            # a file (or a link) where the new version has a folder: Benchtop's own goes, anything else is moved aside
            parts = rel.split(os.sep)[:-1]
            for i in range(1, len(parts) + 1):
                a = os.path.join(*parts[:i])
                p = root / a
                if p.is_symlink() or (p.exists() and not p.is_dir()):
                    drop(a, False) if ours(a) else move_aside(a)
                    break
                if not p.exists():
                    break
            if target.is_dir() and not target.is_symlink():
                move_aside(rel)  # a folder where the new version has a file: never deleted
            elif shipped is not None and not ours(rel) and target.is_file() and not target.is_symlink() \
                    and not filecmp.cmp(new / rel, target, shallow=False):
                move_aside(rel)  # a file of yours where the new version puts one of its own
            changed += _place(new / rel, target)

    # what the new version no longer has: Benchtop's own old files go, yours stay (in a copy that can't tell them
    # apart, they're moved aside instead; a folder holding a database is always left where it is)
    for entry in sorted(root.iterdir()):  # top-level files; folders other than Benchtop's own are never looked in
        rel = entry.name
        if rel in new_files or rel == MANIFEST or rules.kept(rel) or not (entry.is_file() or entry.is_symlink()):
            continue
        if ours(rel):
            drop(rel)
    for top in PROGRAM_DIRS:
        base = root / top
        if not base.is_dir() or base.is_symlink() or rules.kept(top):
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True):
            rel_dir = os.path.relpath(dirpath, root)
            look_in = []
            for d in dirnames:
                rel = os.path.join(rel_dir, d)
                p = root / rel
                if rules.kept(rel) or rules.contains_protected(rel):
                    continue
                if p.is_symlink():  # a link to a folder: the link itself is what's kept or removed
                    if not (new / rel).is_symlink():
                        if ours(rel):
                            drop(rel)
                        elif shipped is None:
                            move_aside(rel)
                elif (new / rel).is_dir() and not (new / rel).is_symlink():
                    look_in.append(d)
                elif d in SKIP_NAMES:
                    shutil.rmtree(p)
                    emptied.add(p.parent)
                elif shipped is not None:
                    look_in.append(d)  # Benchtop's old files in it go, yours stay
                elif not _has_data(p):
                    move_aside(rel)
            dirnames[:] = look_in
            for f in filenames:
                rel = os.path.join(rel_dir, f)
                if rel in new_files or rules.kept(rel):
                    continue
                if ours(rel):
                    drop(rel)
                elif _junk(f):
                    drop(rel, False)
                elif shipped is None and f != "benchtop.db":
                    move_aside(rel)
    for d in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        while d != root and root in d.parents and not rules.kept(os.path.relpath(d, root)):
            if (new / os.path.relpath(d, root)).is_dir() or not d.is_dir() or d.is_symlink() or any(d.iterdir()):
                break
            d.rmdir()
            d = d.parent
    if not (new / MANIFEST).exists() and (root / MANIFEST).exists():
        (root / MANIFEST).unlink()  # it described the version that was here, not this one
    return {"changed": changed, "removed": removed, "kept": aside.moved,
            "kept_in": str(aside.folder) if aside.folder else ""}


def snapshot(root: Path, dest: Path) -> int:
    """Copy the program files of ROOT into DEST: the files its release listed (and the built interface), or, for
    a copy from before releases listed their files, the top-level files and Benchtop's own folders."""
    root = root.resolve()
    rules = _Rules(root)
    n = 0
    shipped = read_manifest(root)
    if shipped is not None:
        dist = root / "frontend/dist"
        built = {os.path.relpath(p, root) for p in dist.rglob("*") if p.is_file()} if dist.is_dir() else set()
        copied = set()
        for rel in sorted(shipped | built):
            p = root / rel
            if rules.kept(rel) or not (p.is_file() or p.is_symlink()):
                continue
            n += _place(p, dest / rel)
            copied.add(rel)
        write_manifest(dest, copied, version_in(root))
        return n
    for entry in sorted(root.iterdir()):
        rel = entry.name
        if rules.kept(rel) or entry.name in SKIP_NAMES:
            continue
        if entry.is_file() or entry.is_symlink():
            n += _place(entry, dest / rel)
        elif entry.is_dir() and rel in PROGRAM_DIRS:
            for dirpath, dirnames, filenames in os.walk(entry):
                rel_dir = os.path.relpath(dirpath, root)
                dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES and not rules.kept(os.path.join(rel_dir, d))]
                for f in filenames:
                    r = os.path.join(rel_dir, f)
                    if f not in SKIP_NAMES and not rules.kept(r):
                        n += _place(root / r, dest / r)
    return n


def main(argv: list[str]) -> int:
    args = argv[1:]
    expect = None
    if len(args) == 5 and args[0] == "mirror" and args[3] == "--expect-version":
        expect, args = args[4], args[:3]
    if len(args) == 3 and args[0] == "mirror":
        try:
            r = mirror(Path(args[1]), Path(args[2]), expect)
        except Refused as e:
            print(f"{e}. Nothing was changed.", file=sys.stderr)
            return 3
        print(f"{r['changed']} files updated, {r['removed']} old ones removed")
        if r["kept"]:
            print(f"{len(r['kept'])} files or folders that aren't part of this version were moved (not deleted) to "
                  f"{r['kept_in']} (they may be yours; Benchtop doesn't need them):")
            for rel in r["kept"][:10]:
                print(f"   {rel}")
            if len(r["kept"]) > 10:
                print(f"   …and {len(r['kept']) - 10} more")
        return 0
    if len(args) == 3 and args[0] == "snapshot":
        snapshot(Path(args[1]), Path(args[2]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
