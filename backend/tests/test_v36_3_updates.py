"""0.36.3: updating safely (a stress test of updating and installing).

Run the real launcher (and deploy/update-server.sh) against stand-ins, as tests/test_v19_updates.py does: a local Git
repository for GitHub, a release list on disk, a stand-in npm, git, rm and docker where a test needs one.

- U1  a data folder given as a relative path (inside backend/) survives rollback, whichever folder it's run from
- U2  an update or rollback never deletes your own files in Benchtop's folders (the release lists its own files)
- U3  building the interface leaves the tracked package-lock.json alone, so the next update isn't refused
- U4  a kept copy left half-removed is never put back (rollback used to empty backend/)
- U5  a lab server whose new version didn't build says so, and the next run builds it
- U6  `upgrade ZIP` refuses a Git copy (it deleted hundreds of tracked files)
- U7  a failed or same-version update keeps the way back from the previous update
- U8  a Git update cut off part-way: nothing runs on the mix, and rollback puts the old version back
- U9  a release tag moved on GitHub, and Git's own message when fetching fails
- U10 one Benchtop per data folder; update and friends see a Benchtop running on another port
- U11 a Python environment whose Python is gone is rebuilt
- U12 only plain release tags on main are taken
- U13 control characters in release notes and commit messages never reach the terminal
- U14 a port in use, a data folder that is a file, a launcher run through a link, a release with no fingerprint,
      an unreadable or rate-limited release list: plain messages
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app import install, updates
from app.config import settings
from tests.test_v19_updates import (  # noqa: F401 - venv, git_lab and zip_lab are fixtures used here
    REPO, Lab, _attach_env, _free_port, _releases_file, _zip_of, git, git_lab, make_program, needs_tools, release,
    set_version, venv, version_of, zip_lab,
)

PREV = ".benchtop-previous"
MANIFEST = ".benchtop-files"
CORE = ("benchtop", "backend/app/__init__.py", "backend/app/main.py", "backend/requirements.txt")


def changes(root: Path) -> str:
    """What Git (and GitHub Desktop) would list as changed, apart from the test's link to its Python environment."""
    return "\n".join(ln for ln in git(root, "status", "--porcelain").splitlines() if "backend/.venv" not in ln)


def run_in(lab: Lab, *args: str, cwd: Path | None = None, ok: bool = True, env: dict | None = None,
           new_session: bool = False, launcher: Path | None = None) -> subprocess.CompletedProcess:
    """./benchtop ARGS, from any folder (Lab.run always runs it from the Benchtop folder)."""
    r = subprocess.run(["bash", str(launcher or lab.root / "benchtop"), *args], cwd=cwd or lab.root, env=env or lab.env,
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, start_new_session=new_session)
    r.out = r.stdout + r.stderr  # type: ignore[attr-defined]
    if ok and r.returncode:
        raise AssertionError(f"./benchtop {' '.join(args)} failed ({r.returncode}):\n{r.out}")
    return r


def newer_zip(src: Path, out: Path, v: str, extra: dict[str, str] | None = None) -> Path:
    set_version(src, v)
    for rel, text in (extra or {}).items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    git(src, "add", "-A")
    git(src, "commit", "-qm", f"v{v}")
    return _zip_of(src, out)


def stand_in(folder: Path, name: str, script: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(script)
    (folder / name).chmod(0o755)
    return folder


# ---------------------------------------------------------------- U1: a relative data folder


@needs_tools
def test_a_relative_data_folder_is_inside_backend_and_survives_rollback_from_any_folder(zip_lab, tmp_path):
    lab, _, _ = zip_lab
    lab.env["BENCHTOP_DATA_DIR"] = "labdata"  # as people write it in settings: Benchtop has always put it in backend/
    lab.data = lab.root / "backend/labdata"
    lab.make_data()
    (lab.data / "settings.env").write_text("BENCHTOP_BACKUP_KEEP=3\n")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    st = run_in(lab, "status", cwd=elsewhere)
    assert f"Data:   {lab.data}" in st.out, st.out  # the same folder Benchtop itself uses, from wherever it's run

    run_in(lab, "update", "--yes", cwd=elsewhere)
    assert version_of(lab.root) == "50.1.0" and lab.my_work() == ["yield 87%"]
    run_in(lab, "rollback", "--yes", cwd=lab.root)
    assert version_of(lab.root) == "50.0.0"
    assert lab.my_work() == ["yield 87%"] and len(lab.backups()) == 1, "the data folder (and its backups) survive"
    assert (lab.data / "settings.env").exists()


def test_the_copier_protects_a_relative_data_folder_whatever_folder_it_runs_in(tmp_path, monkeypatch):
    new, root = tmp_path / "new", tmp_path / "root"
    for p in CORE:
        (new / p).parent.mkdir(parents=True, exist_ok=True)
        (new / p).write_text("x")
    (root / "backend/labdata").mkdir(parents=True)
    (root / "backend/labdata/notes.txt").write_text("mine")
    monkeypatch.setenv("BENCHTOP_DATA_DIR", "labdata")
    monkeypatch.chdir(tmp_path)  # not backend/
    install.mirror(new, root)
    assert (root / "backend/labdata/notes.txt").read_text() == "mine"


# ---------------------------------------------------------------- U2: your own files in Benchtop's folders


def _my_files(root: Path) -> dict[str, str]:
    mine = {"backend/data copy/benchtop.db": "a copy of my database", "backend/data copy/attachments/ab/scan.pdf": "pdf",
            "docs/my-protocol.md": "my protocol", "frontend/my-note.txt": "note", "samples/my-run.jdx": "spectrum",
            "my-notes.txt": "mine"}
    for rel, text in mine.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return mine


@needs_tools
def test_update_and_rollback_leave_your_own_files_in_benchtops_folders(zip_lab, tmp_path):
    lab, new_zip, old_zip = zip_lab
    mine = _my_files(lab.root)
    r = lab.run("update", "--yes")
    for rel, text in mine.items():
        assert (lab.root / rel).exists() and (lab.root / rel).read_text() == text, rel
    assert version_of(lab.root) == "50.1.0" and not (lab.root / "docs/OLD-NOTES.md").exists()  # Benchtop's own old file
    assert (lab.root / MANIFEST).exists()
    assert "moved (not deleted)" not in r.out and not list(lab.root.glob("Benchtop-kept-files-*"))

    lab.run("rollback", "--yes")
    assert version_of(lab.root) == "50.0.0" and (lab.root / "docs/OLD-NOTES.md").exists()
    assert not (lab.root / "backend/app/new_in_50_1.py").exists()
    for rel, text in mine.items():
        assert (lab.root / rel).read_text() == text, rel


@needs_tools
def test_rollback_removes_a_new_top_level_file_of_the_newer_version(zip_lab, tmp_path):
    lab, _, _ = zip_lab
    src = tmp_path / "src"
    z = newer_zip(src, tmp_path / "zips2", "50.2.0", {"CITATION.cff": "cite me\n"})
    lab.run("upgrade", str(z))
    assert (lab.root / "CITATION.cff").exists()
    lab.run("rollback", "--yes")
    assert version_of(lab.root) == "50.0.0" and not (lab.root / "CITATION.cff").exists()
    assert (lab.root / "my-notes.txt").exists()


@needs_tools
def test_a_copy_without_the_list_of_files_moves_unknown_files_aside_never_deletes(zip_lab):
    lab, _, _ = zip_lab
    (lab.root / MANIFEST).unlink(missing_ok=True)  # installed before releases listed their files
    mine = _my_files(lab.root)
    r = lab.run("update", "--yes")
    assert version_of(lab.root) == "50.1.0"
    kept = list(lab.root.glob("Benchtop-kept-files-*"))
    assert (lab.root / "backend/data copy/benchtop.db").read_text() == "a copy of my database"  # a folder with a database stays
    assert len(kept) == 1 and str(kept[0]) in r.out and "moved (not deleted)" in r.out
    assert (kept[0] / "docs/my-protocol.md").read_text() == "my protocol"
    assert (kept[0] / "docs/OLD-NOTES.md").exists()  # can't tell it from yours: kept too
    assert (lab.root / "my-notes.txt").read_text() == "mine"


@needs_tools
def test_the_release_zip_lists_its_own_files(tmp_path):
    src = make_program(tmp_path / "src", "50.0.0")
    (src / "backend/tests/upgrades").mkdir(parents=True)
    (src / "backend/tests/upgrades/v0.7.1.zip").write_bytes(b"left out")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "more")
    z = _zip_of(src, tmp_path / "out")
    with zipfile.ZipFile(z) as zf:
        names = {n[len("benchtop/"):] for n in zf.namelist() if not n.endswith("/")}
        listed = {ln for ln in zf.read(f"benchtop/{MANIFEST}").decode().splitlines() if ln and not ln.startswith("#")}
    assert listed == names - {MANIFEST}
    assert "frontend/dist/index.html" in listed and "benchtop" in listed and not any("tests/upgrades" in n for n in listed)


def test_the_copier_never_deletes_your_files_and_only_removes_what_the_old_version_listed(tmp_path):
    new, root = tmp_path / "new", tmp_path / "root"
    for p in (*CORE, "backend/app/a.py"):
        for base in (new, root):
            (base / p).parent.mkdir(parents=True, exist_ok=True)
            (base / p).write_text("x")
    (new / "docs/guide.md").parent.mkdir(parents=True)
    (new / "docs/guide.md").write_text("new guide")
    for rel in ("backend/app/gone.py", "backend/app/old_pkg/m.py", "OLD-TOP.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("old")
    install.write_manifest(new, {*CORE, "backend/app/a.py", "docs/guide.md"})
    install.write_manifest(root, {*CORE, "backend/app/a.py", "backend/app/gone.py", "backend/app/old_pkg/m.py", "OLD-TOP.txt"})
    (root / "docs").mkdir()
    (root / "docs/guide.md").write_text("my own guide, where the new version puts one")
    (root / "backend/app/old_pkg/mine.txt").write_text("mine")
    (root / "backend/data copy").mkdir()
    (root / "backend/data copy/benchtop.db").write_text("db")
    r = install.mirror(new, root)
    assert not (root / "backend/app/gone.py").exists() and not (root / "backend/app/old_pkg/m.py").exists()
    assert not (root / "OLD-TOP.txt").exists() and r["removed"] == 3
    assert (root / "backend/app/old_pkg/mine.txt").read_text() == "mine" and (root / "backend/data copy/benchtop.db").exists()
    assert (root / "docs/guide.md").read_text() == "new guide"
    assert r["kept"] == ["docs/guide.md"] and (Path(r["kept_in"]) / "docs/guide.md").read_text().startswith("my own guide")


# ---------------------------------------------------------------- U3: package-lock.json


NPM = """#!/bin/sh
# a stand-in npm that behaves as the real one does with the lock file: install writes this version into it
echo "npm $*" >> "$NPM_LOG"
case "$1" in
  install) mkdir -p node_modules
           v="$(sed -nE 's/.*"version": "([^"]+)".*/\\1/p' package.json | head -1)"
           sed "s/\\"version\\": \\"0.10.0\\"/\\"version\\": \\"$v\\"/" package-lock.json > lock.tmp && mv lock.tmp package-lock.json ;;
  ci) [ -f package-lock.json ] || exit 1; rm -rf node_modules; mkdir -p node_modules ;;
  run) mkdir -p dist; echo '<!doctype html><title>Benchtop</title>' > dist/index.html ;;
esac
"""
LOCK = '{\n  "name": "benchtop-frontend",\n  "version": "0.10.0",\n  "lockfileVersion": 3,\n  "packages": {\n    "": {\n      "name": "benchtop-frontend",\n      "version": "0.10.0"\n    }\n  }\n}\n'


@pytest.fixture
def git_hub(tmp_path, venv):
    """GitHub with 50.0.0 (with a package-lock.json kept at 0.10.0, as Benchtop's is), a clone of it, and a way to
    release newer versions."""
    src = make_program(tmp_path / "src", "50.0.0")
    (src / "frontend/package-lock.json").write_text(LOCK)
    git(src, "add", "-A")
    git(src, "commit", "-qm", "the lock")
    git(src, "tag", "-f", "v50.0.0")
    hub = tmp_path / "github.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(hub))
    clone = tmp_path / "benchtop"
    git(tmp_path, "clone", "-q", str(hub), str(clone))
    _attach_env(clone, venv)
    lab = Lab(clone, tmp_path / "data")
    lab.make_data()

    def publish(v: str, extra: dict[str, str] | None = None, tag: bool = True) -> str:
        set_version(src, v)
        for rel, text in (extra or {}).items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        git(src, "add", "-A")
        git(src, "commit", "-qm", f"v{v}: newer")
        if tag:
            git(src, "tag", "-f", f"v{v}")
        git(src, "push", "-q", "--force", str(hub), "main", "--tags")
        return git(src, "rev-parse", "HEAD").strip()

    return lab, src, hub, publish


@needs_tools
def test_building_the_interface_leaves_the_lock_file_alone_so_the_next_update_goes_ahead(git_hub, tmp_path):
    lab, src, hub, publish = git_hub
    shutil.rmtree(lab.root / "frontend/dist")  # the interface needs building (as on a first start, or after an update)
    stand_in(lab.data.parent / "fake-bin", "npm", NPM)
    lab.env["NPM_LOG"] = str(tmp_path / "npm.log")
    publish("50.1.0")
    lab.run("update", "--yes")
    assert version_of(lab.root) == "50.1.0" and (lab.root / "frontend/dist/index.html").exists()
    assert changes(lab.root) == "", "Git sees no change to Benchtop's files"
    publish("50.2.0")
    r = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.1.0 -> 50.2.0" in r.out and changes(lab.root) == ""
    calls = (tmp_path / "npm.log").read_text().splitlines()
    assert calls.count("npm ci --no-audit --no-fund --loglevel=error") == 1, calls  # the lock didn't change: not again
    assert calls.count("npm run build --silent") == 2 and not any("install" in c for c in calls)


@needs_tools
def test_update_puts_back_a_lock_file_an_earlier_build_changed_but_nothing_else(git_hub):
    lab, src, hub, publish = git_hub
    publish("50.1.0")
    lock = lab.root / "frontend/package-lock.json"
    lock.write_text(lock.read_text().replace('"version": "0.10.0"', '"version": "50.0.0"'))  # what npm install did
    r = lab.run("update", "--yes")
    assert "Put frontend/package-lock.json back" in r.out and version_of(lab.root) == "50.1.0"
    assert changes(lab.root) == ""
    publish("50.2.0")
    lock.write_text(lock.read_text().replace('"version": "0.10.0"', '"version": "50.1.0"'))
    main_py = lab.root / "backend/app/main.py"
    main_py.write_text(main_py.read_text() + "\n# my tweak\n")
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "changes that aren't committed" in r.out and version_of(lab.root) == "50.1.0"
    assert "50.1.0" in lock.read_text()  # with your own change in there too, nothing is touched


# ---------------------------------------------------------------- U4: a half-removed kept copy


@needs_tools
def test_rollback_refuses_a_kept_copy_left_half_removed(zip_lab):
    lab, _, _ = zip_lab
    lab.run("update", "--yes")
    shutil.rmtree(lab.root / PREV / "program/backend")  # an update cut off while it removed the kept copy
    r = lab.run("rollback", "--yes", ok=False)
    assert r.returncode and "isn't complete" in r.out and "Nothing was changed" in r.out
    assert version_of(lab.root) == "50.1.0" and (lab.root / "backend/app/main.py").exists()
    assert lab.my_work() == ["yield 87%"]


RM = """#!/bin/bash
# stand-in rm: removing the copy kept for rollback, it removes part of it, then the power goes (the process group dies)
for a in "$@"; do case "$a" in */.benchtop-previous|*/.benchtop-previous.old) /bin/rm -rf "$a/program/backend"; kill -9 0 ;; esac; done
exec /bin/rm "$@"
"""


@needs_tools
def test_an_update_cut_off_while_it_removes_the_old_kept_copy_never_breaks_rollback(zip_lab, tmp_path):
    lab, new_zip, _ = zip_lab
    lab.run("upgrade", str(new_zip))
    newest = newer_zip(tmp_path / "src", tmp_path / "zips3", "50.2.0")
    env = {**lab.env, "PATH": f"{stand_in(tmp_path / 'cut-bin', 'rm', RM)}{os.pathsep}{lab.env['PATH']}"}
    run_in(lab, "upgrade", str(newest), env=env, new_session=True, ok=False)
    assert (lab.root / "backend/app/main.py").exists()
    r = lab.run("rollback", "--yes", ok=False)
    assert (lab.root / "backend/app/main.py").exists() and (lab.root / "backend/app/__init__.py").exists(), r.out
    assert version_of(lab.root) in ("50.0.0", "50.1.0") and lab.my_work() == ["yield 87%"]
    assert "Back on Benchtop" in r.out or "Nothing was changed" in r.out


def test_the_copier_refuses_a_copy_that_isnt_whole(tmp_path):
    new, root = tmp_path / "new", tmp_path / "root"
    (new / "benchtop").parent.mkdir(parents=True)
    (new / "benchtop").write_text("launcher")
    (root / "backend/app").mkdir(parents=True)
    (root / "backend/app/main.py").write_text("keep me")
    with pytest.raises(install.Refused, match="isn't a whole copy"):
        install.mirror(new, root)
    assert (root / "backend/app/main.py").read_text() == "keep me"
    for p in CORE:
        (new / p).parent.mkdir(parents=True, exist_ok=True)
        (new / p).write_text('__version__ = "1.0"\n')
    install.write_manifest(new, {*CORE, "docs/missing.md"})
    with pytest.raises(install.Refused, match="1 of its files are missing"):
        install.mirror(new, root)
    (new / MANIFEST).unlink()
    with pytest.raises(install.Refused, match="not 2.0"):
        install.mirror(new, root, expect_version="2.0")
    assert (root / "backend/app/main.py").read_text() == "keep me"
    assert install.main(["x", "mirror", str(tmp_path / "nothing"), str(root)]) == 3


# ---------------------------------------------------------------- U5: a lab server whose new version didn't build


DOCKER = """#!/bin/bash
# stand-in docker compose: the version the "running container" answers with lives in $FAKE_STATE
st="${FAKE_STATE:?}"; args="$*"
case "$args" in
  *" exec -T app python -c "*) cat "$st" 2>/dev/null ;;
  *" ps -q --status running app"*) echo abc123 ;;
  *" exec -T app python -m app.backup create"*) echo "Backup written: /backups/benchtop-backup-x.zip (1.0 MB; 0 attached files)" ;;
  *" up -d --build"*) if [ -n "${FAKE_BUILD_FAIL:-}" ]; then echo "failed to solve: npm ci: network unreachable" >&2; exit 17; fi
                      sed -nE 's/^__version__ = "([^"]+)".*/\\1/p' backend/app/__init__.py > "$st" ;;
  *" logs "*) echo "app  | INFO started" ;;
esac
"""


@pytest.fixture
def server(git_hub, tmp_path):
    lab, src, hub, publish = git_hub
    shutil.copy2(REPO / "deploy/server.env.example", lab.root / "deploy/.env")
    state = tmp_path / "running-version"
    state.write_text("50.0.0\n")
    env = {"PATH": f"{stand_in(tmp_path / 'docker-bin', 'docker', DOCKER)}{os.pathsep}{os.environ['PATH']}",
           "HOME": str(tmp_path), "LANG": "C.UTF-8", "FAKE_STATE": str(state)}

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess:
        r = subprocess.run(["bash", str(lab.root / "deploy/update-server.sh"), *args], cwd=tmp_path, env={**env, **extra},
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        r.out = r.stdout + r.stderr  # type: ignore[attr-defined]
        return r

    return lab, publish, state, run


@needs_tools
def test_a_server_whose_new_version_didnt_build_says_so_and_the_next_run_builds_it(server):
    lab, publish, state, run = server
    publish("50.1.0")
    r = run("--yes", FAKE_BUILD_FAIL="1")
    assert r.returncode and "didn't build" in r.out and "(50.0.0) still is" in r.out and "--rollback" in r.out
    assert state.read_text().strip() == "50.0.0"
    r = run("--yes")
    assert r.returncode == 0 and "50.0.0 is still running" in r.out and "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out
    assert state.read_text().strip() == "50.1.0"
    assert "newest version (50.1.0)" in run("--yes").out


@needs_tools
def test_a_server_takes_only_plain_release_tags_on_main_and_a_moved_tag(server):
    lab, publish, state, run = server
    publish("50.1.0")
    assert run("--check").returncode == 0  # this copy now has GitHub's v50.1.0
    moved = publish("50.1.0", {"backend/app/fixed.py": "# the release, re-tagged\n"})
    publish("50.2.0", tag=False)
    git(lab.root.parent / "src", "tag", "v50.2.0-beta")
    git(lab.root.parent / "src", "push", "-q", str(lab.root.parent / "github.git"), "--tags")
    r = run("--yes")
    assert r.returncode == 0 and "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out, r.out
    assert git(lab.root, "rev-parse", "HEAD").strip() == moved
    assert (lab.root / "deploy/.previous-version").exists() and not (lab.root / "deploy/.previous-version.new").exists()


# ---------------------------------------------------------------- U6: upgrade ZIP in a Git copy


@needs_tools
def test_upgrade_with_a_zip_refuses_a_git_copy(git_hub, tmp_path):
    lab, src, hub, publish = git_hub
    publish("50.1.0")
    z = _zip_of(src, tmp_path / "zips")
    r = lab.run("upgrade", str(z), ok=False)
    assert r.returncode and "./benchtop update" in r.out and "Nothing was changed" in r.out
    assert version_of(lab.root) == "50.0.0" and changes(lab.root) == "" and not lab.backups()


# ---------------------------------------------------------------- U7: the way back survives a failed update


@needs_tools
def test_a_git_update_that_fails_keeps_the_way_back_from_the_update_before(git_hub):
    lab, src, hub, publish = git_hub
    publish("50.1.0")
    lab.run("update", "--yes")
    publish("50.2.0", {"backend/app/new_in_50_2.py": "# new\n"})
    (lab.root / "backend/app/new_in_50_2.py").write_text("# an untracked file of mine, in the way\n")
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "Nothing was changed" in r.out and version_of(lab.root) == "50.1.0"
    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out and version_of(lab.root) == "50.0.0"


@needs_tools
def test_upgrading_to_the_version_already_installed_is_refused_and_keeps_the_way_back(zip_lab):
    lab, new_zip, _ = zip_lab
    lab.run("update", "--yes")
    r = lab.run("upgrade", str(new_zip), ok=False)
    assert r.returncode and "the version you already have" in r.out and len(lab.backups()) == 1
    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out and version_of(lab.root) == "50.0.0"


# ---------------------------------------------------------------- U8: a Git update cut off part-way


GIT = """#!/bin/bash
# stand-in git: "merge --ff-only T" moves only backend/app to T (as a cut-off checkout leaves it), then the power goes
if [ "$3" = merge ] && [ "$4" = --ff-only ]; then
  "$REAL_GIT" -C "$2" checkout "$6" -- backend/app; : > "$2/.git/index.lock"; kill -9 0
fi
exec "$REAL_GIT" "$@"
"""


@needs_tools
def test_a_git_update_cut_off_part_way_wont_run_and_rollback_puts_the_old_version_back(git_hub, tmp_path):
    lab, src, hub, publish = git_hub
    old_rev = git(lab.root, "rev-parse", "HEAD").strip()
    publish("50.1.0", {"backend/app/new_in_50_1.py": "# new\n"})
    env = {**lab.env, "REAL_GIT": shutil.which("git"),
           "PATH": f"{stand_in(tmp_path / 'cut-git', 'git', GIT)}{os.pathsep}{lab.env['PATH']}"}
    run_in(lab, "update", "--yes", env=env, new_session=True, ok=False)
    assert version_of(lab.root) == "50.1.0" and (lab.root / ".git/index.lock").exists()  # the mix

    for cmd in (["update", "--yes"], ["backup"], ["upgrade", str(tmp_path / "x.zip")]):
        r = lab.run(*cmd, ok=False)
        assert r.returncode == 1 and "was cut off part-way" in r.out and "./benchtop rollback" in r.out, (cmd, r.out)
    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out and version_of(lab.root) == "50.0.0"
    assert git(lab.root, "rev-parse", "HEAD").strip() == old_rev
    assert changes(lab.root) == "" and not (lab.root / ".git/index.lock").exists()
    assert lab.my_work() == ["yield 87%"]
    again = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in again.out and (lab.root / "backend/app/new_in_50_1.py").exists()


# ---------------------------------------------------------------- U9: a moved tag, and Git's own message


@needs_tools
def test_a_release_tag_moved_on_github_doesnt_stop_updates(git_hub):
    lab, src, hub, publish = git_hub
    publish("50.1.0")
    assert "Benchtop 50.1.0 is available" in lab.run("update", "--check").out  # this copy now has the tag
    moved = publish("50.1.0", {"backend/app/fixed.py": "# the release, re-tagged\n"})
    r = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out and git(lab.root, "rev-parse", "HEAD").strip() == moved


@needs_tools
def test_a_failed_fetch_shows_gits_own_message(git_hub, tmp_path):
    lab, src, hub, publish = git_hub
    git(lab.root, "remote", "set-url", "origin", str(tmp_path / "no-such-repository"))
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "does not appear to be a git repository" in r.out and "Nothing was changed" in r.out


# ---------------------------------------------------------------- U10: one Benchtop per data folder


def _serve(lab: Lab, port: int) -> subprocess.Popen:
    return subprocess.Popen([str(lab.root / "backend/.venv/bin/python"), "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=lab.root / "backend",
                            env={**lab.env, "BENCHTOP_PORT": str(port), "BENCHTOP_BACKUP_INTERVAL_HOURS": "0"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)


def _wait_up(port: int, proc: subprocess.Popen) -> None:
    for _ in range(240):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            if proc.poll() is not None:
                raise AssertionError(proc.stdout.read())
            time.sleep(0.25)
    raise AssertionError("Benchtop didn't start")


@needs_tools
def test_update_sees_a_benchtop_running_on_another_port_and_a_second_one_wont_start(zip_lab):
    lab, new_zip, _ = zip_lab
    port = _free_port()
    first = _serve(lab, port)
    second = None
    try:
        _wait_up(port, first)
        second = _serve(lab, _free_port())
        out, _ = second.communicate(timeout=120)
        assert second.returncode and "already running with this data folder" in out and "Traceback" not in out, out
        r = lab.run("start", ok=False)
        assert r.returncode and "already running with this data" in r.out
        for cmd in (["update", "--yes"], ["upgrade", str(new_zip)], ["restore", str(lab.data / "x.zip")]):
            r = lab.run(*cmd, ok=False)
            assert r.returncode and "running" in r.out and str(first.pid) in r.out, (cmd, r.out)
        assert version_of(lab.root) == "50.0.0" and not lab.backups()
    finally:
        if second is not None and second.poll() is None:
            os.killpg(second.pid, signal.SIGKILL)
            second.wait()
        os.killpg(first.pid, signal.SIGKILL)  # as if the power went: the lock goes with the process
        first.wait()
    r = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out


# ---------------------------------------------------------------- U11: a Python environment whose Python is gone


@needs_tools
def test_an_environment_whose_python_is_gone_is_rebuilt(zip_lab, tmp_path):
    lab, _, _ = zip_lab
    (lab.root / "backend/.venv").unlink()  # the shared test environment stays as it is
    real = tmp_path / "gone-python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(lab.root / "backend/.venv")], check=True)
    py = lab.root / "backend/.venv/bin/python"
    for p in py.parent.glob("python*"):
        p.unlink()
        p.symlink_to(real / "python3.99")  # Homebrew moved on: the Python it was made with is no more
    # a stand-in for the newest Python on the computer: makes a real environment (with this test run's packages)
    maker = f"""#!/bin/bash
if [ "$1" = -m ] && [ "$2" = venv ]; then
  "{sys.executable}" -m venv --without-pip --system-site-packages "$3" || exit $?
  (cd "{lab.root}/backend" && printf '%s' "$(cksum < requirements.txt)" > "$3/.requirements-installed"
   printf '%s' "$(cksum < requirements.txt) $(cksum < requirements-pdfa.txt)" > "$3/.requirements-pdfa-tried")
  exit 0
fi
exec "{sys.executable}" "$@"
"""
    env = {**lab.env, "PATH": f"{stand_in(tmp_path / 'py-bin', 'python3.15', maker)}{os.pathsep}{lab.env['PATH']}"}
    r = run_in(lab, "backups", env=env, ok=False)
    assert r.returncode == 0 and "rebuilding the environment" in r.out, r.out
    assert subprocess.run([str(py), "-c", "import sys"]).returncode == 0


# ---------------------------------------------------------------- U12: only plain release tags on main


@needs_tools
def test_update_takes_only_plain_release_tags_on_main(git_hub):
    lab, src, hub, publish = git_hub
    publish("50.1.0")
    publish("50.2.0", tag=False)
    git(src, "tag", "v50.2.0-beta")
    git(src, "switch", "-q", "-c", "side")
    side = publish("50.3.0", tag=False)
    git(src, "tag", "v50.3.0")
    git(src, "push", "-q", str(hub), "--tags")
    git(src, "switch", "-q", "main")
    r = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out, r.out
    assert git(lab.root, "rev-parse", "HEAD").strip() != side and version_of(lab.root) == "50.1.0"


# ---------------------------------------------------------------- U13: control characters


@needs_tools
def test_commit_messages_and_release_notes_reach_the_terminal_without_control_characters(git_hub, tmp_path, monkeypatch, capsys):
    lab, src, hub, publish = git_hub
    set_version(src, "50.1.0")
    git(src, "commit", "-qam", "Faster search \x1b]0;PWNED\x07\x1b[2J done")
    git(src, "tag", "v50.1.0")
    git(src, "push", "-q", str(hub), "main", "--tags")
    out = lab.run("update", "--check").out
    assert "Faster search" in out and "PWNED" in out and "\x1b]0;" not in out and "\x07" not in out and "\x1b[2J" not in out

    from app import __version__
    newer = f"{updates.parse_version(__version__)[0] + 1}.0.0"
    feed = tmp_path / "releases.json"
    feed.write_text(json.dumps([{"tag_name": f"v{newer}", "name": "Benchtop \x1b[31mred", "draft": False, "assets": [],
                                 "body": "### Notes\r\n\r\n- \x1b[2Jcleared\x9b2J\t(tab kept)\n\n---\n\nHow to"}]))
    monkeypatch.setattr(settings, "update_url", feed.as_uri())
    assert updates.main(["x", "check"]) == 0
    printed = capsys.readouterr().out
    assert "cleared" in printed and "\t(tab kept)" in printed and "Benchtop [31mred" in printed
    assert not any(c in printed for c in "\x1b\x9b\r\x07")


# ---------------------------------------------------------------- U14: plain messages


@needs_tools
def test_a_port_in_use_says_so_before_anything_starts(git_hub):
    lab, _, _, _ = git_hub
    with socket.socket() as s:
        s.bind(("127.0.0.1", int(lab.env["BENCHTOP_PORT"])))
        s.listen()
        r = lab.run("start", ok=False)
    port = int(lab.env["BENCHTOP_PORT"])
    assert r.returncode == 1 and f"Port {port} is already used by another program" in r.out
    assert f"BENCHTOP_PORT={port + 1}" in r.out and "Traceback" not in r.out


@needs_tools
def test_a_data_folder_that_is_a_file_gets_a_plain_message(git_hub, tmp_path):
    lab, _, _, _ = git_hub
    f = tmp_path / "not-a-folder"
    f.write_text("x")
    lab.env["BENCHTOP_DATA_DIR"] = str(f)
    r = lab.run("backup", ok=False)
    assert r.returncode == 1 and "that's a file, not a folder" in r.out and "Traceback" not in r.out
    assert f.read_text() == "x"


@needs_tools
def test_the_launcher_works_through_a_link(git_hub, tmp_path):
    lab, _, _, _ = git_hub
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin/benchtop"
    link.symlink_to(os.path.relpath(lab.root / "benchtop", link.parent))  # ln -s ../benchtop/benchtop ~/bin/benchtop
    r = run_in(lab, "status", cwd=tmp_path, launcher=link)
    assert f"Folder: {lab.root}" in r.out and not (tmp_path / "bin/backend").exists()


@needs_tools
def test_a_release_without_a_fingerprint_says_it_couldnt_be_checked(zip_lab):
    lab, new_zip, _ = zip_lab
    feed = Path(lab.env["BENCHTOP_UPDATE_URL"][len("file://"):])
    rel = json.loads(feed.read_text())
    rel[0]["assets"][0].pop("digest")
    feed.write_text(json.dumps(rel))
    r = lab.run("update", "--yes")
    assert "couldn't be checked" in r.out and version_of(lab.root) == "50.1.0"


class _RateLimited(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"message": "API rate limit exceeded for 1.2.3.4."}'
        self.send_response(403)
        self.send_header("X-RateLimit-Remaining", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_an_unreadable_or_rate_limited_release_list_says_what_happened(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "releases.json"
    bad.write_text("<html>not a list</html>")
    monkeypatch.setattr(settings, "update_url", bad.as_uri())
    assert updates.main(["x", "check"]) == 3
    err = capsys.readouterr().err
    assert "couldn't be read" in err and "internet connection" not in err
    srv = http.server.HTTPServer(("127.0.0.1", 0), _RateLimited)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(settings, "update_url", f"http://127.0.0.1:{srv.server_address[1]}/releases")
        assert updates.main(["x", "check"]) == 3
        err = capsys.readouterr().err
        assert "rate limit" in err and "Try again in an hour" in err and "internet connection" not in err
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_launchers_stay_bash_3_2_friendly():
    for f in ("benchtop", "deploy/update-server.sh"):
        text = (REPO / f).read_text()
        for bash4 in (",,}", "^^}", "declare -A", "mapfile", "readarray", "readlink -f", "&>>", "|&"):
            assert bash4 not in text, (f, bash4)
        if shutil.which("bash"):
            subprocess.run(["bash", "--posix", "-n", str(REPO / f)], check=True)


def test_the_data_lock_lets_go_when_benchtop_stops(tmp_path):
    from app import datalock
    datalock.acquire(tmp_path, 8000)
    datalock.acquire(tmp_path, 8000)  # the same process again: fine
    r = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; from app import datalock\n"
                        "try:\n    datalock.acquire(Path(sys.argv[1]))\nexcept datalock.InUse as e:\n    print(e)",
                        str(tmp_path)], cwd=REPO / "backend", capture_output=True, text=True)
    assert f"process {os.getpid()}" in r.stdout
    datalock.release(tmp_path)
    r = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; from app import datalock\n"
                        "datalock.acquire(Path(sys.argv[1])); print('got it')", str(tmp_path)],
                       cwd=REPO / "backend", capture_output=True, text=True)
    assert "got it" in r.stdout
