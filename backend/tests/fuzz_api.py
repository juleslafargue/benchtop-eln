"""API fuzzing: thousands of odd but well-formed requests to every endpoint, looking for server errors.

Schemathesis reads Benchtop's API description (/openapi.json) and generates requests from it: empty values, huge
numbers, strange Unicode, missing fields, IDs that don't exist. Benchtop should answer each one with a result or a
plain refusal (400, 404, 422…), never "Internal Server Error". Each signed-in role works on its own copy of the demo
lab: a scientist, an admin and a read-only viewer, plus requests with no sign-in at all. Any error in the server's
log counts too. Used by the GitHub workflow; to run it yourself (from backend/):

    pip install -r requirements-dev.txt
    python tests/fuzz_api.py                    # the same requests as GitHub (a fixed seed)
    python tests/fuzz_api.py --seed random      # new ones each time: worth a run now and then
    python tests/fuzz_api.py --examples 100     # more requests per endpoint (default 25)

A failure prints a curl command that reproduces it. Each one gets a fix and a regression test (0.24.1 found five).
"""
from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
PW = "demo-lab-password"
ROLES = [("scientist", "mruiz"), ("admin", "hweiss"), ("viewer", "ohaddad"), ("no sign-in", None)]
# left out, with the reason:
SKIP = "|".join([
    r"^/api/auth/(logout|me/password|sessions)",  # would sign the fuzzer itself out, so the rest of the run tests nothing
    r"^/api/users/\{uid\}/sign-out",  # the same, when it picks the admin's own account
    r"^/api/webhooks/\{hook_id\}/test", r"^/api/admin/(email|timestamps)/test",  # contact outside servers
    r"^/api/lookup", r"/safety/refresh$",  # PubChem: rightly "503 turned off" here, as the fuzzing lab works offline
])
OFFLINE = {"BENCHTOP_PUBCHEM": "off", "BENCHTOP_DOI": "off", "BENCHTOP_NMR_AUTO_UPDATE": "off", "BENCHTOP_BACKUP_INTERVAL_HOURS": "0",
           "BENCHTOP_UPDATE_CHECK": "off", "BENCHTOP_TIMESTAMPS": "off", "BENCHTOP_INSTRUMENT_SCAN_SECONDS": "0",
           "BENCHTOP_CHECK_INTERVAL_HOURS": "0",
           "BENCHTOP_ZENODO": "off"}  # publishing (0.28) is refused plainly, and nothing is sent to Zenodo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fuzz(role: str, user: str | None, pristine: Path, root: Path, seed: int, examples: int) -> tuple[bool, str]:
    data = root / role.replace(" ", "-")
    shutil.copytree(pristine, data)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = root / f"{data.name}-server.log"
    env = {**os.environ, **OFFLINE, "BENCHTOP_DATA_DIR": str(data)}
    with open(log, "w") as f:  # a file, not a pipe: an unread pipe fills up and stalls the server
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=BACKEND, env=env,
                                  stdout=f, stderr=subprocess.STDOUT)
    try:
        for _ in range(240):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            return False, "the server didn't start:\n" + log.read_text()[-3000:]
        auth = []
        if user:
            tok = httpx.post(base + "/api/auth/login", json={"username": user, "password": PW}).json()["token"]
            auth = ["-H", f"Authorization: Bearer {tok}"]
        r = subprocess.run(["schemathesis", "run", base + "/openapi.json", *auth, "--checks", "not_a_server_error",
                            "--phases", "examples,coverage,fuzzing", "--max-examples", str(examples), "--seed", str(seed),
                            "--workers", "4", "--request-timeout", "60", "--exclude-path-regex", SKIP, "--no-color"],
                           capture_output=True, text=True, timeout=3600)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
    out = r.stdout + r.stderr
    tracebacks = log.read_text().count("Traceback (most recent call last)")
    made = re.search(r"(\d+) generated", out)
    summary = f"{made.group(1) if made else '?'} requests"
    if r.returncode == 0 and not tracebacks:
        return True, summary
    report = out[out.find("FAILURES"):] if "FAILURES" in out else out[-6000:]
    if tracebacks:
        report += f"\n{tracebacks} error(s) in the server log ({log}):\n" + log.read_text()[-4000:]
    return False, summary + "\n" + report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", default="20261025", help='a number (the same requests every time), or "random"')
    ap.add_argument("--examples", type=int, default=25, help="requests per endpoint and phase (default 25)")
    args = ap.parse_args(argv[1:])
    seed = random.randrange(1 << 31) if args.seed == "random" else int(args.seed)
    root = Path(tempfile.mkdtemp(prefix="benchtop-fuzz-"))
    pristine = root / "demo-lab"
    print(f"Building the demo lab… (seed {seed}; to repeat this run: python tests/fuzz_api.py --seed {seed})", flush=True)
    subprocess.run([sys.executable, "-m", "app.demo_lab"], cwd=BACKEND, env={**os.environ, **OFFLINE, "BENCHTOP_DATA_DIR": str(pristine)},
                   check=True, stdout=subprocess.DEVNULL)
    failed = 0
    for role, user in ROLES:
        print(f"Fuzzing as {role}…", flush=True)
        ok, report = fuzz(role, user, pristine, root, seed, args.examples)
        print(("  ✓ " if ok else "  ✗ ") + report, flush=True)
        failed += not ok
    print("API fuzzing:", "passed" if not failed else f"problems found as {failed} role(s); each one above has a curl command to reproduce it")
    if not failed:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
