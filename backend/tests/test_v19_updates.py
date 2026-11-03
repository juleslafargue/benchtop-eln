"""0.19: easier updates.

- ./benchtop update, end to end, for both kinds of install: a Git clone (moves to the newest release tag) and an
  unzipped release (downloads the newest release zip and checks its fingerprint); and ./benchtop rollback after each.
  These run the real launcher against a stand-in for GitHub: a local Git repository, and a release list on disk.
- what update refuses to do: while Benchtop runs, with changed program files, off main, a damaged download, an older zip
- the copier never touches data, settings or files of yours in the Benchtop folder
- the daily "is there a newer version?" check and the admin's dashboard notice
- the release builder: zip layout (one benchtop/ folder, ready-built interface, runnable launchers) and notes
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import install, updates
from app.config import settings

REPO = Path(__file__).resolve().parents[2]
HAVE_TOOLS = all(shutil.which(t) for t in ("git", "unzip", "curl", "bash"))
needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="needs git, unzip, curl and bash")


def _load_release():
    spec = importlib.util.spec_from_file_location("benchtop_release", REPO / "deploy/release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


release = _load_release()

# ---------------------------------------------------------------- a small Benchtop to update


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout


def set_version(root: Path, v: str) -> None:
    p = root / "backend/app/__init__.py"
    p.write_text(re.sub(r'^__version__ = ".*"', f'__version__ = "{v}"', p.read_text(), flags=re.M))
    pkg = root / "frontend/package.json"
    pkg.write_text(re.sub(r'"version": "[^"]+"', f'"version": "{v}"', pkg.read_text(), count=1))


def version_of(root: Path) -> str:
    return re.search(r'^__version__ = "([^"]+)"', (root / "backend/app/__init__.py").read_text(), re.M).group(1)


def _built_ui(root: Path) -> None:
    dist = root / "frontend/dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>Benchtop</title>")
    later = time.time() + 60  # built after the sources, as a release's is
    os.utime(dist / "index.html", (later, later))


def make_program(dest: Path, v: str) -> Path:
    """The parts of Benchtop the launcher needs, as a Git repository at version v."""
    dest.mkdir(parents=True)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    for f in ("benchtop", "Benchtop.command", ".gitignore", "backend/requirements.txt", "frontend/package.json"):
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, dest / f)
    for d in ("backend/app", "frontend/src", "deploy", "docs"):
        shutil.copytree(REPO / d, dest / d, ignore=ign)
    (dest / ".github/workflows").mkdir(parents=True)
    shutil.copy2(REPO / "deploy/github-tests.yml", dest / ".github/workflows/tests.yml")
    set_version(dest, v)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", f"v{v}: the version you have")
    git(dest, "tag", f"v{v}")
    return dest


@pytest.fixture(scope="module")
def venv(tmp_path_factory) -> Path:
    """One Python environment for every copy (using this test run's packages, so nothing is downloaded)."""
    d = tmp_path_factory.mktemp("venv") / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(d)], check=True)
    return d


def _attach_env(root: Path, venv: Path, build_ui: bool = True) -> None:
    (root / "backend/.venv").symlink_to(venv)
    want = subprocess.run("cksum < requirements.txt", shell=True, cwd=root / "backend", capture_output=True, text=True).stdout.strip()
    (venv / ".requirements-installed").write_text(want)  # the same requirements everywhere: "already installed"
    if build_ui:
        _built_ui(root)


class Lab:
    """A copy of Benchtop with its own data, run through the real launcher."""

    def __init__(self, root: Path, data: Path, releases: Path | None = None):
        self.root, self.data = root, data
        # a stand-in npm that fails loudly: none of these updates should need to rebuild the interface
        fake = data.parent / "fake-bin"
        fake.mkdir(exist_ok=True)
        (fake / "npm").write_text("#!/bin/sh\necho 'npm was called: the launcher thought the interface needed rebuilding' >&2\nexit 97\n")
        (fake / "npm").chmod(0o755)
        self.env = {
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "HOME": str(data.parent), "LANG": "C.UTF-8",
            "BENCHTOP_DATA_DIR": str(data), "BENCHTOP_PORT": str(_free_port()), "BENCHTOP_DEMO_PORT": str(_free_port()),
            "BENCHTOP_NMR_AUTO_UPDATE": "off", "BENCHTOP_PUBCHEM": "off", "BENCHTOP_TIMESTAMPS": "off",
            "BENCHTOP_UPDATE_URL": releases.as_uri() if releases else "file:///nonexistent",
        }

    def run(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["bash", str(self.root / "benchtop"), *args], cwd=self.root, env=self.env,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        if ok and r.returncode:
            raise AssertionError(f"./benchtop {' '.join(args)} failed ({r.returncode}):\n{out}")
        r.out = out  # type: ignore[attr-defined]
        return r

    def make_data(self) -> None:
        subprocess.run([str(self.root / "backend/.venv/bin/python"), "-c",
                        "from app.db import init_db, engine\nfrom sqlalchemy import text\ninit_db()\n"
                        "with engine.begin() as c: c.execute(text(\"CREATE TABLE my_work (x TEXT)\")); "
                        "c.execute(text(\"INSERT INTO my_work VALUES ('yield 87%')\"))"],
                       cwd=self.root / "backend", env=self.env, check=True, capture_output=True)

    def my_work(self) -> list[str]:
        import sqlite3
        with sqlite3.connect(self.data / "benchtop.db") as c:
            return [r[0] for r in c.execute("SELECT x FROM my_work")]

    def backups(self) -> list[Path]:
        return sorted((self.data / "backups").glob("benchtop-backup-*.zip"))


@pytest.fixture
def git_lab(tmp_path, venv):
    """GitHub (a bare repository) with 50.0.0 and a newer 50.1.0, and a clone of it at 50.0.0."""
    src = make_program(tmp_path / "src", "50.0.0")
    hub = tmp_path / "github.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(hub))
    clone = tmp_path / "benchtop"
    git(tmp_path, "clone", "-q", str(hub), str(clone))
    _attach_env(clone, venv)
    set_version(src, "50.1.0")
    (src / "backend/app/new_in_50_1.py").write_text("# new\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "v50.1.0: a newer version\n\n- Something better\n\nCo-Authored-By: X <x@example.invalid>")
    git(src, "tag", "v50.1.0")
    git(src, "push", "-q", str(hub), "main", "--tags")
    lab = Lab(clone, tmp_path / "data")
    lab.make_data()
    return lab, src, hub


# ---------------------------------------------------------------- Git installs


@needs_tools
def test_git_install_updates_to_the_newest_release_and_rolls_back(git_lab):
    lab, _, _ = git_lab
    old_rev = git(lab.root, "rev-parse", "HEAD").strip()
    check = lab.run("update", "--check")
    assert "Benchtop 50.1.0 is available (you have 50.0.0)" in check.out and "v50.1.0: a newer version" in check.out
    assert version_of(lab.root) == "50.0.0" and not lab.backups()  # --check only looks

    r = lab.run("update", "--yes")
    assert "Backing up your data first" in r.out and "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out
    assert version_of(lab.root) == "50.1.0" and (lab.root / "backend/app/new_in_50_1.py").exists()
    assert len(lab.backups()) == 1 and str(lab.backups()[0]) in r.out
    assert git(lab.root, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert lab.my_work() == ["yield 87%"]
    assert "You have the newest version (50.1.0)" in lab.run("update", "--yes").out

    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out
    assert version_of(lab.root) == "50.0.0" and not (lab.root / "backend/app/new_in_50_1.py").exists()
    assert git(lab.root, "rev-parse", "HEAD").strip() == old_rev
    assert lab.my_work() == ["yield 87%"] and not (lab.root / ".benchtop-previous").exists()
    assert "no earlier version" in lab.run("rollback", "--yes", ok=False).out


@needs_tools
def test_git_update_takes_the_release_tag_not_untested_work_on_main(git_lab):
    lab, src, hub = git_lab
    (src / "backend/app/half_done.py").write_text("# not released yet\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "work in progress")
    git(src, "push", "-q", str(hub), "main")
    lab.run("update", "--yes")
    assert version_of(lab.root) == "50.1.0" and not (lab.root / "backend/app/half_done.py").exists()


@needs_tools
def test_git_update_refuses_changed_program_files_another_branch_and_a_running_benchtop(git_lab):
    lab, _, _ = git_lab
    main_py = lab.root / "backend/app/main.py"
    main_py.write_text(main_py.read_text() + "\n# my tweak\n")
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "changes that aren't committed" in r.out and "backend/app/main.py" in r.out
    assert version_of(lab.root) == "50.0.0" and not lab.backups()
    git(lab.root, "checkout", "-q", "--", "backend/app/main.py")

    git(lab.root, "switch", "-q", "-c", "my-experiment")
    r = lab.run("update", "--yes", ok=False)
    assert "on 'my-experiment', not main" in r.out and version_of(lab.root) == "50.0.0"
    git(lab.root, "switch", "-q", "main")

    with _pretend_benchtop_runs(int(lab.env["BENCHTOP_PORT"])):
        r = lab.run("update", "--yes", ok=False)
    assert "Benchtop is running" in r.out and version_of(lab.root) == "50.0.0" and not lab.backups()


@needs_tools
def test_update_asks_first_and_does_nothing_without_a_yes(git_lab):
    lab, _, _ = git_lab
    r = lab.run("update", ok=False)  # not a terminal, no --yes: it explains rather than guessing
    assert r.returncode and "--yes" in r.out and version_of(lab.root) == "50.0.0" and not lab.backups()


@needs_tools
def test_rollback_explains_when_the_older_version_cant_open_the_data(git_lab):
    lab, _, _ = git_lab
    lab.run("update", "--yes")
    import sqlite3
    with sqlite3.connect(lab.data / "benchtop.db") as c:  # as if 50.1.0 had upgraded the database
        c.execute("INSERT INTO benchtop_schema VALUES (999, 'a newer database layout', '50.1.0', '2026-10-21')")
    r = lab.run("rollback", "--yes", ok=False)
    assert r.returncode == 1 and "can't open your data as 50.1.0 left it" in r.out
    assert "./benchtop restore" in r.out and str(lab.backups()[0]) in r.out and "./benchtop update" in r.out
    assert lab.my_work() == ["yield 87%"]  # nothing restored behind the person's back


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true, "version": "50.0.0"}')

    def log_message(self, *a):
        pass


class _pretend_benchtop_runs:
    def __init__(self, port: int):
        self.srv = http.server.HTTPServer(("127.0.0.1", port), _Health)

    def __enter__(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------- zip installs


def _zip_of(src: Path, out: Path) -> Path:
    _built_ui(src)
    return release.build_zip(src, version_of(src), out)


def _releases_file(path: Path, zips: dict[str, Path], digest: str | None = None) -> Path:
    rel = []
    for v, z in zips.items():
        sha = digest if digest is not None else hashlib.sha256(z.read_bytes()).hexdigest()
        rel.append({"tag_name": f"v{v}", "name": f"Benchtop {v}", "html_url": f"https://example.invalid/releases/tag/v{v}",
                    "body": f"Notes for {v}", "published_at": "2026-10-21T00:00:00Z", "draft": False, "prerelease": False,
                    "assets": [{"name": z.name, "browser_download_url": z.as_uri(), "digest": f"sha256:{sha}"}]})
    path.write_text(json.dumps(rel))
    return path


@pytest.fixture
def zip_lab(tmp_path, venv):
    src = make_program(tmp_path / "src", "50.0.0")
    (src / "docs/OLD-NOTES.md").write_text("gone in 50.1.0\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "an extra doc")
    old_zip = _zip_of(src, tmp_path / "zips")
    with zipfile.ZipFile(old_zip) as z:
        z.extractall(tmp_path / "unpacked")
    root = tmp_path / "benchtop"
    (tmp_path / "unpacked/benchtop").rename(root)
    for f in ("benchtop", "Benchtop.command"):
        (root / f).chmod(0o755)
    # unzipped days after the release was built, as people do: the folders are dated then (0.18 rebuilt the
    # interface on every fresh zip install because of this, which needs Node)
    later = time.time() + 3 * 86400
    for d in [root, *[p for p in root.rglob("*") if p.is_dir()]]:
        os.utime(d, (later, later))
    _attach_env(root, venv, build_ui=False)  # the zip's own interface, with the zip's dates
    (root / "my-notes.txt").write_text("mine\n")  # things people keep in the Benchtop folder
    (root / "my-backups").mkdir()
    (root / "my-backups/precious.zip").write_text("x")
    set_version(src, "50.1.0")
    (src / "backend/app/new_in_50_1.py").write_text("# new\n")
    git(src, "rm", "-q", "docs/OLD-NOTES.md")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "v50.1.0: newer")
    new_zip = _zip_of(src, tmp_path / "zips")
    rel = _releases_file(tmp_path / "releases.json", {"50.1.0": new_zip, "50.0.0": old_zip})
    lab = Lab(root, tmp_path / "data", rel)
    lab.make_data()
    return lab, new_zip, old_zip


@needs_tools
def test_zip_install_downloads_checks_and_installs_the_newest_release_then_rolls_back(zip_lab):
    lab, _, _ = zip_lab
    assert not (lab.root / ".git").exists()
    r = lab.run("update", "--yes")
    assert "Benchtop 50.1.0 is available (you have 50.0.0)" in r.out and "Notes for 50.1.0" in r.out
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out and len(lab.backups()) == 1
    assert version_of(lab.root) == "50.1.0"
    assert (lab.root / "backend/app/new_in_50_1.py").exists() and not (lab.root / "docs/OLD-NOTES.md").exists()
    assert (lab.root / "my-notes.txt").read_text() == "mine\n" and (lab.root / "my-backups/precious.zip").exists()
    assert os.access(lab.root / "benchtop", os.X_OK) and lab.my_work() == ["yield 87%"]
    assert "You have the newest version (50.1.0)" in lab.run("update", "--yes").out

    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out and version_of(lab.root) == "50.0.0"
    assert not (lab.root / "backend/app/new_in_50_1.py").exists() and (lab.root / "docs/OLD-NOTES.md").exists()
    assert (lab.root / "my-notes.txt").exists() and (lab.root / "my-backups/precious.zip").exists()
    assert lab.my_work() == ["yield 87%"] and (lab.root / "frontend/dist/index.html").exists()


@needs_tools
def test_zip_update_refuses_a_damaged_download(zip_lab, tmp_path):
    lab, new_zip, old_zip = zip_lab
    _releases_file(Path(lab.env["BENCHTOP_UPDATE_URL"][len("file://"):]), {"50.1.0": new_zip}, digest="0" * 64)
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "damaged" in r.out and version_of(lab.root) == "50.0.0" and not lab.backups()


@needs_tools
def test_zip_update_offline_says_so_and_changes_nothing(zip_lab):
    lab, _, _ = zip_lab
    lab.env["BENCHTOP_UPDATE_URL"] = "file:///nonexistent/releases.json"
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode and "Couldn't reach GitHub" in r.out and version_of(lab.root) == "50.0.0"


@needs_tools
def test_upgrade_accepts_any_single_top_folder_and_refuses_an_older_zip(zip_lab, tmp_path):
    lab, new_zip, old_zip = zip_lab
    # GitHub's green "Code → Download ZIP" button names the folder benchtop-eln-main (and has no built interface)
    github_style = tmp_path / "benchtop-eln-main.zip"
    with zipfile.ZipFile(new_zip) as zin, zipfile.ZipFile(github_style, "w") as zout:
        for i in zin.infolist():
            i2 = zipfile.ZipInfo(i.filename.replace("benchtop/", "benchtop-eln-main/", 1), i.date_time)
            i2.external_attr = i.external_attr
            zout.writestr(i2, zin.read(i))
    r = lab.run("upgrade", str(github_style))
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in r.out and version_of(lab.root) == "50.1.0"
    r = lab.run("upgrade", str(old_zip), ok=False)
    assert r.returncode and "older than this one" in r.out and "rollback" in r.out and version_of(lab.root) == "50.1.0"
    bogus = tmp_path / "photos.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("a/b.jpg", "x")
        z.writestr("c/d.jpg", "x")
    r = lab.run("upgrade", str(bogus), ok=False)
    assert "doesn't look like a Benchtop release" in r.out and version_of(lab.root) == "50.1.0"


# ---------------------------------------------------------------- the copier


def test_mirror_replaces_program_files_but_never_data_settings_or_your_files(tmp_path, monkeypatch):
    new, root = tmp_path / "new", tmp_path / "root"
    for p, text in {"benchtop": "new launcher", "backend/app/a.py": "new a", "frontend/dist/index.html": "new ui",
                    "deploy/x.yml": "new"}.items():
        (new / p).parent.mkdir(parents=True, exist_ok=True)
        (new / p).write_text(text)
    for p, text in {"benchtop": "old launcher", "backend/app/a.py": "old a", "backend/app/gone.py": "old",
                    "backend/app/old_pkg/m.py": "old", "backend/data/benchtop.db": "DATA", "backend/data/settings.env": "S=1",
                    "backend/.venv/bin/python": "py", "frontend/node_modules/x/i.js": "nm", "deploy/.env": "SECRET=1",
                    "deploy/backups/b.zip": "B", "my-notes.txt": "mine", "elsewhere/backups/z.zip": "Z",
                    ".git/HEAD": "ref", "backend/backups-here/k.zip": "K"}.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(text)
    monkeypatch.setenv("BENCHTOP_BACKUP_DIR", str(root / "backend/backups-here"))  # a backup folder set inside it
    inode = (root / "benchtop").stat().st_ino
    r = install.mirror(new, root)
    t = lambda p: (root / p).read_text()  # noqa: E731
    assert t("benchtop") == "new launcher" and t("backend/app/a.py") == "new a" and t("frontend/dist/index.html") == "new ui"
    assert (root / "benchtop").stat().st_ino != inode  # renamed into place: a running launcher keeps reading the old one
    assert not (root / "backend/app/gone.py").exists() and not (root / "backend/app/old_pkg").exists()
    for p, text in {"backend/data/benchtop.db": "DATA", "backend/data/settings.env": "S=1", "backend/.venv/bin/python": "py",
                    "frontend/node_modules/x/i.js": "nm", "deploy/.env": "SECRET=1", "deploy/backups/b.zip": "B",
                    "my-notes.txt": "mine", "elsewhere/backups/z.zip": "Z", ".git/HEAD": "ref",
                    "backend/backups-here/k.zip": "K"}.items():
        assert t(p) == text, p
    assert r["removed"] == 2
    snap = tmp_path / "snap"
    install.snapshot(root, snap)
    assert (snap / "benchtop").exists() and (snap / "backend/app/a.py").exists() and (snap / "my-notes.txt").exists()
    assert not (snap / "backend/data").exists() and not (snap / "backend/.venv").exists() and not (snap / ".git").exists()
    assert not (snap / "deploy/.env").exists() and not (snap / "elsewhere").exists() and not (snap / "backend/backups-here").exists()


# ---------------------------------------------------------------- the daily check and the dashboard notice


def _gh(tmp_path: Path, versions: list[str], **extra) -> Path:
    rel = [{"tag_name": f"v{v}", "name": f"Benchtop {v}", "html_url": f"https://github.com/x/y/releases/tag/v{v}",
            "body": f"Notes {v}", "draft": False, "prerelease": False, "assets": [], **extra} for v in versions]
    p = tmp_path / "releases.json"
    p.write_text(json.dumps(rel))
    return p


def test_versions_compare_as_numbers():
    assert updates.parse_version("v0.19.0") == (0, 19, 0) and updates.parse_version("0.19.0-beta") is None
    assert updates.is_newer("0.19.0", "0.18.3") and updates.is_newer("0.18.10", "0.18.9") and updates.is_newer("1.0", "0.99.99")
    assert not updates.is_newer("0.18.3", "0.18.3") and not updates.is_newer("0.18.3.0", "0.18.3") and not updates.is_newer("junk", "0.1")


def test_check_remembers_newer_releases_and_skips_drafts_and_prereleases(tmp_path, monkeypatch):
    from app import __version__
    major = updates.parse_version(__version__)[0]
    newer, newest = f"{major + 1}.0.0", f"{major + 1}.1.0"
    rel = json.loads(_gh(tmp_path, [newest, newer, __version__, "0.1.0"]).read_text())
    rel.append({"tag_name": f"v{major + 9}.0.0", "draft": True, "assets": []})
    rel.append({"tag_name": f"v{major + 8}.0.0", "prerelease": True, "assets": []})
    p = tmp_path / "r.json"
    p.write_text(json.dumps(rel))
    monkeypatch.setattr(settings, "update_url", p.as_uri())
    monkeypatch.setattr(settings, "install_kind", "git")
    st = updates.check()
    assert st["available"]["version"] == newest and st["versions_behind"] == 2 and st["command"] == "./benchtop update"
    assert st["available"]["url"].endswith(f"/v{newest}") and st["error"] is None


def test_offline_check_keeps_the_last_answer_and_notes_the_error(tmp_path, monkeypatch):
    from app import __version__
    newer = f"{updates.parse_version(__version__)[0] + 1}.0.0"
    monkeypatch.setattr(settings, "update_url", _gh(tmp_path, [newer]).as_uri())
    updates.check()
    monkeypatch.setattr(settings, "update_url", (tmp_path / "missing.json").as_uri())
    st = updates.check()
    assert st["available"]["version"] == newer and st["error"]


def test_daily_check_runs_once_a_day_only_when_switched_on(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(updates, "fetch_releases", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(settings, "update_check", False)
    assert updates.maybe_check() is None and not calls  # BENCHTOP_UPDATE_CHECK=off: never looks
    monkeypatch.setattr(settings, "update_check", True)
    updates._path().unlink(missing_ok=True)
    now = datetime.now(timezone.utc)
    assert updates.maybe_check(now) is not None and len(calls) == 1
    assert updates.maybe_check(now + timedelta(hours=5)) is None and len(calls) == 1
    assert updates.maybe_check(now + timedelta(hours=24)) is not None and len(calls) == 2
    monkeypatch.setattr(settings, "demo", True)
    assert updates.maybe_check(now + timedelta(days=5)) is None and len(calls) == 2


def test_notice_disappears_once_updated(tmp_path, monkeypatch):
    from app import __version__
    newer = f"{updates.parse_version(__version__)[0] + 1}.0.0"
    monkeypatch.setattr(settings, "update_url", _gh(tmp_path, [newer]).as_uri())
    assert updates.check()["available"]
    monkeypatch.setattr(updates, "__version__", newer)  # as if ./benchtop update had run, before the next daily check
    assert updates.status()["available"] is None


def test_admin_dashboard_shows_the_notice_and_nobody_else_does(client, admin, users, tmp_path, monkeypatch):
    admin_h, user_h = admin, users["alice"]
    from app import __version__
    newer = f"{updates.parse_version(__version__)[0] + 1}.0.0"
    monkeypatch.setattr(settings, "update_url", _gh(tmp_path, [newer]).as_uri())
    monkeypatch.setattr(settings, "install_kind", "docker")
    updates.check()
    sys_admin = client.get("/api/dashboard", headers=admin_h).json()["system"]
    assert sys_admin["update"]["available"]["version"] == newer and sys_admin["update"]["command"] == "deploy/update-server.sh"
    assert client.get("/api/dashboard", headers=user_h).json()["system"]["update"] is None
    monkeypatch.setattr(settings, "update_url", _gh(tmp_path, [__version__]).as_uri())
    updates.check()
    assert client.get("/api/dashboard", headers=admin_h).json()["system"]["update"] is None


def test_a_broken_saved_check_never_breaks_the_dashboard(client, admin):
    admin_h = admin
    updates._path().write_text("{not json")
    assert client.get("/api/dashboard", headers=admin_h).status_code == 200
    updates._path().write_text(json.dumps({"releases": [{"version": None}, "junk"]}))
    assert client.get("/api/dashboard", headers=admin_h).json()["system"]["update"] is None
    updates._path().unlink()


# ---------------------------------------------------------------- the release builder


@needs_tools
def test_release_zip_has_one_benchtop_folder_the_built_interface_and_runnable_launchers(tmp_path):
    src = make_program(tmp_path / "src", "50.0.0")
    (src / ".github/workflows/extra.yml").write_text("x")
    (src / "backend/tests/upgrades").mkdir(parents=True)
    (src / "backend/tests/upgrades/v0.7.1.zip").write_bytes(b"an old version's backup, for the upgrade test")
    (src / "backend/tests/test_v2.py").write_text("# the tests themselves stay in\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "more")
    _built_ui(src)
    z = release.build_zip(src, "50.0.0", tmp_path / "out")
    assert z.name == "benchtop-eln-v50.0.0.zip"
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in zf.infolist()}
        dates = {i.filename: i.date_time for i in zf.infolist()}
    assert all(n.startswith("benchtop/") for n in names)
    assert "benchtop/frontend/dist/index.html" in names and "benchtop/backend/app/__init__.py" in names
    assert not any(n.startswith("benchtop/.github/") for n in names)
    assert not any("tests/upgrades" in n for n in names) and "benchtop/backend/tests/test_v2.py" in names  # 0.24: tests only
    assert modes["benchtop/benchtop"] & 0o111 and modes["benchtop/Benchtop.command"] & 0o111
    assert dates["benchtop/frontend/dist/index.html"] >= dates["benchtop/frontend/src/main.tsx"]


@needs_tools
def test_release_notes_list_the_commits_since_the_last_release_without_trailers(tmp_path):
    src = make_program(tmp_path / "src", "50.0.0")
    first = release.notes(src, "50.0.0")
    assert first.startswith("### The version you have") and "./benchtop update" in first
    for msg in ("Fix the yield chart\n\n- Medians were off by one",
                "v50.1.0: easier updates\n\n- ./benchtop update\n\nCo-Authored-By: Someone <s@example.invalid>\nClaude-Session: https://example.invalid/x"):
        (src / "docs" / f"{len(msg)}.md").write_text(msg)
        git(src, "add", "-A")
        git(src, "commit", "-qm", msg)
    set_version(src, "50.1.0")
    n = release.notes(src, "50.1.0")
    assert "### Easier updates\n\n- ./benchtop update" in n and "### Fix the yield chart\n\n- Medians were off by one" in n
    assert "Co-Authored-By" not in n and "Claude-Session" not in n and "The version you have" not in n
    assert n.index("Medians were off") < n.index("\n---\n") < n.index("**To update:**")


def test_release_notes_never_mention_a_github_account():
    # 0.30.1: "@nn" in a commit message linked a stranger's GitHub account on the v0.29.0 release page
    m = release.no_mentions
    assert m("- @mentions: accented letters work (@nn picks Nadia).") == "- `@mentions`: accented letters work (`@nn` picks Nadia)."
    assert m("Typing @jo. at the end") == "Typing `@jo`. at the end"
    assert m("Already code: `@nn` and `type @bob here`") == "Already code: `@nn` and `type @bob here`"
    assert m("Mail Jules@lafargue.name or s@example.invalid") == "Mail Jules@lafargue.name or s@example.invalid"
    assert m("A lone @ sign, @-1 and 50 @ 2 g") == "A lone @ sign, @-1 and 50 @ 2 g"


@needs_tools
def test_release_notes_put_an_at_word_from_any_commit_in_code(tmp_path):
    src = make_program(tmp_path / "src", "50.0.0")
    for msg in ("Mentions work (work in progress)\n\n- @mentions with accents; @nn picks Nadia",
                "v50.1.0: mentions\n\n- Fixed\n\nCo-Authored-By: Someone <s@example.invalid>"):
        (src / "docs" / f"{len(msg)}.md").write_text(msg)
        git(src, "add", "-A")
        git(src, "commit", "-qm", msg)
    set_version(src, "50.1.0")
    n = release.notes(src, "50.1.0")
    assert "- `@mentions` with accents; `@nn` picks Nadia" in n
    assert re.search(r"(?<![\w`])@\w", n) is None


def test_terminal_shows_what_changed_not_how_to_install(tmp_path, monkeypatch, capsys):
    from app import __version__
    newer = f"{updates.parse_version(__version__)[0] + 1}.0.0"
    p = _gh(tmp_path, [newer])
    rel = json.loads(p.read_text())
    rel[0]["body"] = "### Faster search\n\n- Results in half the time\n\n---\n\n**To update:** run this and that"
    p.write_text(json.dumps(rel))
    monkeypatch.setattr(settings, "update_url", p.as_uri())
    assert updates.main(["x", "check"]) == 0
    out = capsys.readouterr().out
    assert "Results in half the time" in out and "To update" not in out


def test_release_refuses_when_the_two_version_numbers_disagree(tmp_path):
    root = tmp_path / "r"
    (root / "backend/app").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "backend/app/__init__.py").write_text('__version__ = "1.2.3"\n')
    (root / "frontend/package.json").write_text('{"version": "1.2.2"}')
    with pytest.raises(SystemExit, match="disagree"):
        release.version(root)


def test_this_versions_numbers_agree():
    v = release.version(REPO)
    assert updates.parse_version(v), "the version must be plain numbers, so update checks can compare it"


def test_release_job_runs_only_after_every_test_and_only_on_main():
    import yaml
    wf = yaml.safe_load((REPO / "deploy/github-tests.yml").read_text())
    job = wf["jobs"]["release"]
    assert set(job["needs"]) == set(wf["jobs"]) - {"release"}
    assert "refs/heads/main" in job["if"] and "push" in job["if"]
    assert job["permissions"] == {"contents": "write"}


def test_update_server_script_is_runnable_and_valid_bash():
    p = REPO / "deploy/update-server.sh"
    assert os.access(p, os.X_OK)
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(p)], check=True)
        subprocess.run(["bash", "-n", str(REPO / "benchtop")], check=True)
