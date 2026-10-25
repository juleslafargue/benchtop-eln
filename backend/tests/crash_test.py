"""Pull-the-plug tests: Benchtop killed at random moments, then started again. Nothing saved may be lost or broken.

Each round kills the server (or the command) without warning, as a power cut, a crash or a closed laptop would, at a
random moment, then starts it again and checks:

  saves     six scientists save sections, add files, sign records. Every save Benchtop said was done must still be
            there, word for word; a save cut off part-way is either all there or not there at all; every attached
            file opens with the right fingerprint (also when the upload is tried again after the restart); and
            ./benchtop check passes (database, audit trail, signatures, files).
  backups   the same, while an admin makes backup after backup: every finished backup must restore and pass its
            check, and the next backup must work.
  upgrades  Benchtop starting for the first time on an old version's data (0.9.3), killed while it backs up and
            upgrades the database, perhaps more than once: when it finally starts, every row from before is still
            there and unchanged, and the check passes.
  restore   ./benchtop restore killed part-way: Benchtop's data afterwards is either the data from before or the
            backup's, complete, never an empty or half-restored lab.

From backend/:
    python tests/crash_test.py                      # every part (a few minutes)
    python tests/crash_test.py saves --rounds 50    # one part, more rounds
    python tests/crash_test.py --seed 1234          # repeat a run (the seed is printed at the start)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fuzz_api import OFFLINE, PW, free_port

BACKEND = Path(__file__).resolve().parents[1]
WRITERS = ["mruiz", "pnair", "dcho", "slaurent", "amorgan", "kwatanabe"]
OLD_VERSION = "v0.9.3"


class Problem(AssertionError):
    pass


def env_for(data: Path, **extra: str) -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("BENCHTOP_")}
    return {**e, **OFFLINE, "BENCHTOP_DATA_DIR": str(data), "PYTHONDONTWRITEBYTECODE": "1", **extra}


def run(args: list[str], data: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=BACKEND, env=env_for(data, **extra), capture_output=True, text=True, check=False,
                          timeout=900)


def check(data: Path, what: str) -> None:
    r = run(["-m", "app.check"], data)
    if r.returncode != 0:
        raise Problem(f"./benchtop check failed {what}:\n{r.stdout}{r.stderr}")


class Server:
    def __init__(self, data: Path, log: Path, **extra: str):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log
        with open(log, "a") as f:  # a file, not a pipe: an unread pipe fills up and stalls the server
            f.write(f"\n===== start {time.strftime('%H:%M:%S')}\n")
            f.flush()
            self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
                                         cwd=BACKEND, env=env_for(data, **extra), stdout=f, stderr=subprocess.STDOUT)

    def ready(self, timeout: float = 120) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise Problem(f"Benchtop didn't start (exit {self.proc.returncode}):\n{self.log.read_text()[-4000:]}")
            try:
                if httpx.get(self.base + "/api/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise Problem("Benchtop didn't start within two minutes:\n" + self.log.read_text()[-4000:])

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)  # no warning, no clean-up: as a power cut
        self.proc.wait()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.kill()


def login(base: str, user: str) -> dict[str, str]:
    r = httpx.post(base + "/api/auth/login", json={"username": user, "password": PW}, timeout=30)
    if r.status_code != 200:
        raise Problem(f"{user} can't sign in after the restart: {r.status_code} {r.text[:300]}")
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ------------------------------------------------------------------ saves

class Ledger:
    """What Benchtop said it saved (acknowledged), and what was cut off part-way (in flight when it was killed)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.notebooks: dict[str, int] = {}
        self.experiments: dict[int, dict] = {}  # id -> {"user", "title", "entry", "signed": True/False/None (maybe)}
        self.texts: dict[int, dict] = {}  # entry id -> {"acked": html, "maybe": [html…]}
        self.files: dict[int, str] = {}  # attachment id -> sha256
        self.retry: list[tuple[str, int, bytes]] = []  # uploads cut off: tried again after the restart
        self.saves = 0


def _work(base: str, user: str, ledger: Ledger, rng: random.Random, stop: threading.Event, go: threading.Barrier) -> None:
    c = httpx.Client(base_url=base, headers=login(base, user), timeout=60)
    go.wait()  # the kill timer starts once everyone is signed in and working
    try:
        with ledger.lock:
            retry = [r for r in ledger.retry if r[0] == user]
            ledger.retry = [r for r in ledger.retry if r[0] != user]
        for _, exp_id, data in retry:  # the person tries the upload again once Benchtop is back
            _upload(c, user, exp_id, data, ledger)
        if user not in ledger.notebooks:
            r = c.post("/api/notebooks", json={"name": f"Crash test ({user})"})
            r.raise_for_status()
            ledger.notebooks[user] = r.json()["id"]
        while not stop.is_set():
            mine = [i for i, e in ledger.experiments.items() if e["user"] == user and e["signed"] is False]
            roll = rng.random()
            if not mine or roll < 0.15:
                title = f"Crash test {user} {rng.randrange(10**9)}"
                r = c.post("/api/experiments", json={"title": title, "notebook_id": ledger.notebooks[user]})
                r.raise_for_status()
                exp = r.json()
                r = c.post(f"/api/experiments/{exp['id']}/entries", json={"kind": "text", "title": "Notes",
                                                                         "content": {"html": "<p>first</p>"}})
                r.raise_for_status()
                with ledger.lock:
                    ledger.experiments[exp["id"]] = {"user": user, "title": title, "entry": r.json()["id"], "signed": False}
                    ledger.texts[r.json()["id"]] = {"acked": "<p>first</p>", "maybe": []}
                    ledger.saves += 3
            elif roll < 0.70:
                eid = ledger.experiments[rng.choice(mine)]["entry"]
                html = f"<p>{user} {rng.randrange(10**12)} " + "x" * rng.randrange(20000) + "</p>"
                with ledger.lock:
                    ledger.texts[eid]["maybe"].append(html)
                r = c.put(f"/api/entries/{eid}", json={"content": {"html": html}})
                r.raise_for_status()
                with ledger.lock:
                    ledger.texts[eid] = {"acked": html, "maybe": []}
                    ledger.saves += 1
            elif roll < 0.95:
                n = rng.choice([200, 50_000, 2_000_000, 12_000_000])
                _upload(c, user, rng.choice(mine), rng.randbytes(n), ledger)
            else:
                exp_id = rng.choice(mine)
                with ledger.lock:
                    ledger.experiments[exp_id]["signed"] = None  # maybe, until Benchtop answers
                r = c.post(f"/api/experiments/{exp_id}/sign", json={"password": PW, "comment": "crash test"})
                r.raise_for_status()
                with ledger.lock:
                    ledger.experiments[exp_id]["signed"] = True
                    ledger.saves += 1
    except httpx.HTTPStatusError as exc:
        if not stop.is_set():
            raise Problem(f"{user}: {exc.request.method} {exc.request.url.path} -> {exc.response.status_code} "
                          f"{exc.response.text[:300]}") from None
    except httpx.HTTPError as exc:
        if not stop.is_set():  # a request that failed while the server was still running
            raise Problem(f"{user}: {exc.__class__.__name__} {exc} ({exc.request.method} {exc.request.url.path})") from None
    finally:
        c.close()


def _upload(c: httpx.Client, user: str, exp_id: int, data: bytes, ledger: Ledger) -> None:
    try:
        r = c.post(f"/api/experiments/{exp_id}/attachments", files={"file": ("data.bin", data, "application/octet-stream")})
    except httpx.HTTPError:
        with ledger.lock:
            ledger.retry.append((user, exp_id, data))
        raise
    r.raise_for_status()
    a = r.json()
    with ledger.lock:
        ledger.files[a["id"]] = a["sha256"]
        ledger.saves += 1
    got = c.get(f"/api/attachments/{a['id']}")  # straight away: does the file just saved open?
    if got.status_code != 200 or hashlib.sha256(got.content).hexdigest() != hashlib.sha256(data).hexdigest():
        raise Problem(f"An attached file ({len(data):,} bytes) was saved but doesn't open: {got.status_code} {got.text[:200]}")


def verify(base: str, ledger: Ledger) -> None:
    """Everything Benchtop said it saved is there, unchanged; anything cut off is all there or not at all."""
    heads = {u: login(base, u) for u in WRITERS}
    with httpx.Client(base_url=base, timeout=120) as c:
        for exp_id, e in list(ledger.experiments.items()):
            r = c.get(f"/api/experiments/{exp_id}", headers=heads[e["user"]])
            if r.status_code != 200:
                raise Problem(f"Experiment {exp_id} ({e['title']}) was saved but is gone: {r.status_code}")
            exp = r.json()
            if exp["title"] != e["title"]:
                raise Problem(f"Experiment {exp_id}'s title changed: {exp['title']!r}")
            signed = exp["status"] in ("signed", "witnessed")
            if e["signed"] is True and not signed:
                raise Problem(f"{exp['eid']} was signed, but after the restart it's {exp['status']}")
            if e["signed"] is False and signed:
                raise Problem(f"{exp['eid']} is signed, but nobody signed it")
            e["signed"] = signed
            entry = next(x for x in exp["entries"] if x["id"] == e["entry"])
            t = ledger.texts[e["entry"]]
            html = entry["content"].get("html")
            if html != t["acked"] and html not in t["maybe"]:
                raise Problem(f"{exp['eid']}'s Notes after the restart are neither the last saved version nor the one "
                              f"cut off: {str(html)[:80]!r} (saved: {t['acked'][:80]!r})")
            ledger.texts[e["entry"]] = {"acked": html, "maybe": []}
        for att_id, sha in list(ledger.files.items()):
            owner = next(e["user"] for e in ledger.experiments.values())  # anyone in the lab can read them
            r = c.get(f"/api/attachments/{att_id}", headers=heads[owner])
            if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != sha:
                raise Problem(f"Attached file {att_id} was saved but doesn't open after the restart: {r.status_code} "
                              f"{r.text[:200] if r.status_code != 200 else '(different content)'}")


def saves(root: Path, pristine: Path, rounds: int, rng: random.Random, backups: bool = False) -> str:
    data = root / ("backups-lab" if backups else "saves-lab")
    shutil.copytree(pristine, data)
    log = root / f"{data.name}-server.log"
    ledger = Ledger()
    zips_ok: set[str] = set()
    for n in range(1, rounds + 1):
        srv = Server(data, log)
        srv.ready()
        if n > 1:
            verify(srv.base, ledger)
            if backups:
                _check_backups(data, zips_ok)
        stop = threading.Event()
        go = threading.Barrier(len(WRITERS) + 1 + backups, timeout=120)
        workers = [threading.Thread(target=_work, args=(srv.base, u, ledger, random.Random(rng.random()), stop, go))
                   for u in WRITERS]
        if backups:
            workers.append(threading.Thread(target=_backups, args=(srv.base, stop, go)))
        errors: list[BaseException] = []
        for w in workers:
            w.run = _catching(w.run, errors)
            w.start()
        go.wait()
        time.sleep(rng.uniform(0.2, 3.0))
        try:  # still answering while everyone works? (0.24.1 could freeze for 30 s when two uploads met)
            httpx.get(srv.base + "/api/health", timeout=5).raise_for_status()
        except httpx.HTTPError as exc:
            stop.set()
            srv.kill()
            raise Problem(f"Round {n}: Benchtop stopped answering while people worked ({exc.__class__.__name__})") from None
        stop.set()  # first, so a request cut off by the kill isn't mistaken for a failure
        srv.kill()
        for w in workers:
            w.join(timeout=120)
        if errors:
            raise errors[0]
        print(f"    round {n}: killed after {ledger.saves:,} saves so far", flush=True)
    srv = Server(data, log)
    srv.ready()
    try:
        verify(srv.base, ledger)
        if backups:
            _check_backups(data, zips_ok)
            r = httpx.post(srv.base + "/api/admin/backups", headers=login(srv.base, "hweiss"), timeout=600)
            if r.status_code != 201:
                raise Problem(f"The next backup after the restarts failed: {r.status_code} {r.text[:300]}")
    finally:
        srv.stop()
    check(data, "after the last restart")
    if "Traceback" in log.read_text():
        raise Problem(f"Errors in the server log ({log}):\n" + log.read_text()[-4000:])
    extra = f", {len(zips_ok)} backups checked" if backups else ""
    return (f"{rounds} kills, {ledger.saves:,} saves, {len(ledger.experiments)} experiments, {len(ledger.files)} files"
            f"{extra}: nothing lost")


def _catching(fn, errors):
    def wrapped():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            errors.append(exc)
    return wrapped


def _backups(base: str, stop: threading.Event, go: threading.Barrier) -> None:
    h = login(base, "hweiss")
    go.wait()
    try:
        while not stop.is_set():
            r = httpx.post(base + "/api/admin/backups", headers=h, timeout=600)
            if r.status_code != 201 and not stop.is_set():
                raise Problem(f"A backup failed: {r.status_code} {r.text[:300]}")
    except (httpx.HTTPError, Problem) as exc:
        if isinstance(exc, Problem):
            raise


def _check_backups(data: Path, done: set[str]) -> None:
    """Every backup with a finished name restores and passes its check (the zip and its files beside it)."""
    for z in sorted((data / "backups").glob("benchtop-backup-*.zip")):
        if z.name in done:
            continue
        r = run(["-m", "app.check", str(z)], data)
        if r.returncode != 0:
            raise Problem(f"A backup made before a kill doesn't pass its check ({z.name}):\n{r.stdout}{r.stderr}")
        done.add(z.name)


# ------------------------------------------------------------------ upgrades

def _old_lab(root: Path) -> Path:
    """The 0.9.3 demo lab, restored with today's ./benchtop restore but not yet started (so not yet upgraded)."""
    from test_v24_upgrades import place_backup
    zp = place_backup(OLD_VERSION, root / "old-backup")
    data = root / "old-lab"
    r = run(["-m", "app.backup", "restore", str(zp)], data)
    if r.returncode != 0:
        raise Problem(r.stdout + r.stderr)
    return data


def _rows(db: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    from test_v24_upgrades import tables
    return tables(db)


def upgrades(root: Path, rounds: int, rng: random.Random) -> str:
    pristine = _old_lab(root)
    before = _rows(pristine / "benchtop.db")
    log = root / "upgrades-server.log"
    shutil.copytree(pristine, root / "timing")
    started = time.monotonic()
    srv = Server(root / "timing", log, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
    srv.ready()
    took = time.monotonic() - started
    srv.stop()
    kills = 0
    for n in range(1, rounds + 1):
        data = root / f"upgrade-{n}"
        shutil.copytree(pristine, data)
        for _ in range(rng.choice([1, 1, 2, 3])):  # killed once, or again while it tries once more
            srv = Server(data, log, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
            time.sleep(rng.uniform(0, took))
            srv.kill()
            kills += 1
        srv = Server(data, log, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
        srv.ready()
        login(srv.base, "mruiz")
        srv.stop()
        after = _rows(data / "benchtop.db")
        for t, (cols, rows) in before.items():
            if t == "benchtop_schema":
                continue
            acols, arows = after.get(t, ([], []))
            keep = [acols.index(c) for c in cols]
            if [tuple(a[k] for k in keep) for a in arows[:len(rows)]] != rows or len(arows) < len(rows):
                raise Problem(f"Upgrade round {n}: table {t} lost or changed rows ({len(rows)} before, {len(arows)} after)")
        check(data, f"after upgrade round {n}")
        made = list((data / "backups").glob("benchtop-backup-*.zip"))
        if not made:
            raise Problem(f"Upgrade round {n}: the database was upgraded without the backup that comes first")
        for z in made:
            r = run(["-m", "app.check", str(z)], data)
            if r.returncode != 0:
                raise Problem(f"Upgrade round {n}: the backup before the upgrade doesn't pass its check:\n{r.stdout}")
        shutil.rmtree(data)
    if "Traceback" in log.read_text():
        raise Problem(f"Errors in the server log ({log}):\n" + log.read_text()[-4000:])
    return f"{rounds} upgrades of the {OLD_VERSION} lab, {kills} kills (a start takes {took:.1f} s): nothing lost"


# ------------------------------------------------------------------ restore

def _fingerprint(data: Path) -> str:
    """What a lab holds: its experiments' titles and its attached files (a digest, to compare two labs)."""
    db = data / "benchtop.db"
    if not db.exists():
        return "no database"
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            rows = c.execute("SELECT id, title FROM experiment ORDER BY id").fetchall()
            files = c.execute("SELECT DISTINCT sha256 FROM attachment ORDER BY sha256").fetchall()
    except sqlite3.Error as exc:
        return f"unreadable database ({exc})"
    return hashlib.sha256(json.dumps([rows, files]).encode()).hexdigest()


def restore(root: Path, pristine: Path, rounds: int, rng: random.Random) -> str:
    base = root / "restore-base"
    shutil.copytree(pristine, base)
    r = run(["-m", "app.backup", "create"], base, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
    if r.returncode != 0:
        raise Problem(r.stdout + r.stderr)
    zp = next((base / "backups").glob("benchtop-backup-*.zip"))
    # after the backup: a new experiment with a new attached file, so the two labs differ in records and files
    srv = Server(base, root / "restore-server.log")
    srv.ready()
    h = login(srv.base, "mruiz")
    nb = httpx.post(srv.base + "/api/notebooks", headers=h, json={"name": "After the backup"}).json()
    exp = httpx.post(srv.base + "/api/experiments", headers=h, json={"title": "After the backup", "notebook_id": nb["id"]}).json()
    httpx.post(srv.base + f"/api/experiments/{exp['id']}/attachments", headers=h, timeout=60,
               files={"file": ("later.bin", random.Random(1).randbytes(3_000_000))}).raise_for_status()
    srv.stop()
    now_fp = _fingerprint(base)
    tmp = root / "restore-timing"
    shutil.copytree(base, tmp)
    started = time.monotonic()
    r = run(["-m", "app.backup", "restore", str(zp)], tmp)
    took = time.monotonic() - started
    if r.returncode != 0:
        raise Problem(r.stdout + r.stderr)
    backup_fp = _fingerprint(tmp)
    shutil.rmtree(tmp)
    seen = {"before": 0, "restored": 0}
    for n in range(1, rounds + 1):
        data = root / f"restore-{n}"
        shutil.copytree(base, data)
        p = subprocess.Popen([sys.executable, "-m", "app.backup", "restore", str(zp)], cwd=BACKEND, env=env_for(data),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(rng.uniform(0, took))
        p.send_signal(signal.SIGKILL)
        p.wait()
        srv = Server(data, root / "restore-server.log")  # Benchtop started again afterwards, as anyone would
        try:
            srv.ready()
            login(srv.base, "mruiz")
        finally:
            srv.stop()
        fp = _fingerprint(data)
        if fp not in (now_fp, backup_fp):
            raise Problem(f"Restore round {n} (killed after {took:.2f} s at most): the lab afterwards is neither the data "
                          f"from before nor the backup's: {fp}")
        seen["before" if fp == now_fp else "restored"] += 1
        check(data, f"after restore round {n}")
        shutil.rmtree(data)
    return (f"{rounds} restores killed part-way (a restore takes {took:.2f} s): {seen['before']} left the data as it was, "
            f"{seen['restored']} finished; never half-done")


# ------------------------------------------------------------------ main

PARTS = ("saves", "backups", "upgrades", "restore")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("parts", nargs="*", help=f"which parts: {', '.join(PARTS)} (default: all)")
    ap.add_argument("--rounds", type=int, default=0, help="kills per part (default 25 saves, 10 backups, 8 upgrades, 30 restores)")
    ap.add_argument("--seed", default="random", help='a number to repeat a run (default "random")')
    args = ap.parse_args(argv[1:])
    if set(args.parts) - set(PARTS):
        ap.error(f"parts are {', '.join(PARTS)}")
    seed = random.randrange(1 << 31) if args.seed == "random" else int(args.seed)
    rng = random.Random(seed)
    parts = args.parts or list(PARTS)
    root = Path(tempfile.mkdtemp(prefix="benchtop-crash-"))
    print(f"Pull-the-plug tests (seed {seed}; to repeat: python tests/crash_test.py --seed {seed})", flush=True)
    pristine = root / "demo-lab"
    if {"saves", "backups", "restore"} & set(parts):
        print("Building the demo lab…", flush=True)
        subprocess.run([sys.executable, "-m", "app.demo_lab"], cwd=BACKEND, env=env_for(pristine), check=True,
                       stdout=subprocess.DEVNULL)
    failed = 0
    for part in parts:
        print(f"{part}…", flush=True)
        try:
            if part == "saves":
                out = saves(root, pristine, args.rounds or 25, rng)
            elif part == "backups":
                out = saves(root, pristine, args.rounds or 10, rng, backups=True)
            elif part == "upgrades":
                out = upgrades(root, args.rounds or 8, rng)
            else:
                out = restore(root, pristine, args.rounds or 30, rng)
            print(f"  ✓ {out}", flush=True)
        except Problem as exc:
            failed += 1
            print(f"  ✗ {exc}", flush=True)
    print("Pull-the-plug tests:", "passed" if not failed else f"{failed} part(s) failed (the files are kept in {root})")
    if not failed:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
