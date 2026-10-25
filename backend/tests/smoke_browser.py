"""Browser smoke test: build the demo lab, start Benchtop, open every page as a real browser would.

Fails on anything a person would notice: a JavaScript error, a server error, a request the page
expected to work coming back refused, or a page wider than the screen. Runs on desktop, phone and
dark mode. Used by the GitHub workflow; to run it yourself (needs the built frontend and Playwright):

    cd backend
    pip install -r requirements-dev.txt && python -m playwright install chromium
    python tests/smoke_browser.py

On data from an older version, upgraded by this one (the oldest demo lab is 0.9.3; see tests/test_v24_upgrades.py):

    python tests/smoke_browser.py --upgraded v0.9.3
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
DIST = BACKEND.parent / "frontend" / "dist"
PORT = int(os.environ.get("SMOKE_PORT", "8765"))
B = f"http://127.0.0.1:{PORT}"
PW = "demo-lab-password"
# requests the sandbox or CI may block that aren't Benchtop's (web fonts, PubChem pictures)
IGNORE = ("fonts.googleapis", "fonts.gstatic", "pubchem.ncbi", "ERR_TUNNEL", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED")


def main(argv: list[str]) -> int:
    if not (DIST / "index.html").exists():
        print(f"Build the frontend first (cd frontend && npm run build); {DIST} is missing")
        return 2
    from playwright.sync_api import sync_playwright

    data = Path(tempfile.mkdtemp(prefix="benchtop-smoke-"))
    env = {**os.environ, "BENCHTOP_DATA_DIR": str(data), "BENCHTOP_DEMO": "1", "BENCHTOP_PUBCHEM": "off",
           "BENCHTOP_NMR_AUTO_UPDATE": "off", "BENCHTOP_BACKUP_INTERVAL_HOURS": "0", "BENCHTOP_STATIC_DIR": str(DIST)}
    if len(argv) > 2 and argv[1] == "--upgraded":
        sys.path.insert(0, str(BACKEND / "tests"))
        from test_v24_upgrades import restore_and_upgrade
        print(f"Restoring the {argv[2]} demo lab and upgrading it…", flush=True)
        data = restore_and_upgrade(argv[2], data)["data"]
        env["BENCHTOP_DATA_DIR"] = str(data)
    else:
        print("Building the demo lab…", flush=True)
        subprocess.run([sys.executable, "-m", "app.demo_lab"], cwd=BACKEND, env=env, check=True, stdout=subprocess.DEVNULL)
    log_path = data / "server.log"
    log_file = open(log_path, "w")  # a file, not a pipe: an unread pipe fills up and stalls the server
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=BACKEND, env=env,
                              stdout=log_file, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(120):
            try:
                if httpx.get(B + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("Server didn't start:\n" + log_path.read_text())
            return 2
        problems = sweep(sync_playwright)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log_file.close()
    out = log_path.read_text()
    tracebacks = out.count("Traceback (most recent call last)") if out else 0
    if tracebacks:
        problems.append(("server", "", "traceback", f"{tracebacks} error(s) in the server log"))
        print(out[-4000:])
    for p in problems:
        print("✗", " | ".join(p))
    print("Browser smoke test:", "passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def sweep(sync_playwright) -> list[tuple[str, str, str, str]]:
    tok = {u: httpx.post(B + "/api/auth/login", json={"username": u, "password": PW}).json()["token"] for u in ("slaurent", "hweiss")}
    H = {"Authorization": "Bearer " + tok["slaurent"]}
    exps = [e for e in httpx.get(B + "/api/experiments?limit=1000", headers=H).json()]
    mats = httpx.get(B + "/api/materials", headers=H).json()
    mats = mats["items"] if isinstance(mats, dict) else mats
    nbs = httpx.get(B + "/api/notebooks", headers=H).json()
    pages = (["/", "/templates", "/materials", f"/materials/{mats[0]['id']}", "/inventory", "/search?q=suzuki", "/reactions",
              "/compounds", "/account", "/groups"] + [f"/notebooks/{n['id']}" for n in nbs]
             + [f"/experiments/{e['id']}" for e in exps] + [f"/bench/{exps[0]['id']}", "/inbox"])
    # Admin's tabs (0.26), each at its own address
    ADMIN = ["/admin"] + [f"/admin/{t}" for t in ("backups", "instruments", "email", "signing", "integrations", "audit")]
    problems: list[tuple[str, str, str, str]] = []
    print(f"Opening {len(pages)} pages on desktop, phone and dark mode…", flush=True)
    runs = [("desktop", "slaurent", {"width": 1400, "height": 900}, "light", pages),
            ("phone", "slaurent", {"width": 390, "height": 844}, "light", pages),
            ("dark", "slaurent", {"width": 1400, "height": 900}, "dark", pages[:12]),
            ("admin", "hweiss", {"width": 1400, "height": 900}, "light", ADMIN + ["/"]),
            ("admin phone", "hweiss", {"width": 390, "height": 844}, "light", ADMIN + ["/"]),  # 0.24: Admin was too wide
            ("admin dark", "hweiss", {"width": 1400, "height": 900}, "dark", ADMIN)]
    # every colour theme, light and dark, on a few busy pages (the phone run covers narrow screens)
    themed = ["/", pages[10 + len(nbs)] if len(pages) > 10 + len(nbs) else "/", "/account", "/reactions"]
    for palette in ("notebook", "steel", "litmus", "contrast"):
        for scheme in ("light", "dark"):
            runs.append((f"theme {palette} {scheme}", "slaurent", {"width": 1300, "height": 850}, scheme, themed, palette))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, user, viewport, scheme, paths, *rest in runs:
            palette = rest[0] if rest else "benchtop"
            httpx.patch(B + "/api/auth/me", headers={"Authorization": "Bearer " + tok[user]}, json={"preferences": {"palette": palette}})
            phone = viewport["width"] < 600
            ctx = browser.new_context(viewport=viewport, color_scheme=scheme, is_mobile=phone, has_touch=phone)
            pg = ctx.new_page()
            cur = {"path": ""}

            def note(kind: str, text: str) -> None:
                if not any(i in text for i in IGNORE):
                    problems.append((label, cur["path"], kind, text[:200]))
            pg.on("console", lambda m: m.type == "error" and note("console error", m.text))
            pg.on("pageerror", lambda e: note("page error", str(e)))
            pg.on("response", lambda r: "/api/" in r.url and (r.status >= 500 or r.status in (401, 403, 404, 422))
                  and note(f"HTTP {r.status}", r.url.replace(B, "")))
            pg.goto(B)
            pg.evaluate(f"localStorage.setItem('benchtop.token', '{tok[user]}')")
            if label == "desktop":  # Chrome must consider Benchtop installable as an app (manifest, icons)
                cdp = ctx.new_cdp_session(pg)
                errs = cdp.send("Page.getInstallabilityErrors").get("installabilityErrors", [])
                if errs:
                    note("installable", "; ".join(e.get("errorId", "?") for e in errs))
            for path in paths:
                cur["path"] = path
                pg.goto(B + path, wait_until="domcontentloaded")
                try:
                    pg.wait_for_load_state("networkidle", timeout=20000)
                except Exception:  # noqa: BLE001
                    note("slow", "still loading after 20 s")
                pg.wait_for_timeout(300)
                if not pg.inner_text("body").strip():
                    note("blank page", "nothing was displayed")
                shown = pg.evaluate("document.documentElement.dataset.palette || 'benchtop'")
                if shown != palette:
                    note("theme", f"expected the {palette} theme, page shows {shown}")
                if path.startswith("/experiments/") and not phone:
                    # the sidebar is part of the page: it scrolls with everything else, never in a box of its own
                    # (0.20.0 pinned it so its lower half was out of reach; 0.20.1 made it a small scrolling menu)
                    bad = pg.evaluate("""() => {
                        const r = document.querySelector('.rail'); if (!r) return ''
                        if (r.scrollHeight > r.clientHeight + 1) return 'it has a scrollbar of its own'
                        const room = document.documentElement.scrollHeight - innerHeight
                        if (room < 50) return ''
                        const before = r.getBoundingClientRect().top
                        window.scrollTo(0, room); const moved = before - r.getBoundingClientRect().top
                        window.scrollTo(0, 0)
                        return Math.abs(moved - room) > 2 ? `it moved ${Math.round(moved)}px when the page scrolled ${Math.round(room)}px` : '' }""")
                    if bad:
                        note("sidebar", f"the sidebar doesn't scroll with the page: {bad}")
                over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if over > 2:
                    note("too wide", f"page is {over}px wider than the screen")
            ctx.close()
        browser.close()
    return problems


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
