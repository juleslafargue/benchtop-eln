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
    problems = serve(env, log_path, lambda: sweep(sync_playwright))
    if problems is None:
        return 2
    # 0.28: publishing on Zenodo. The demo lab never publishes, so the same data is opened again by an ordinary
    # Benchtop, pointed at a fake Zenodo (tests/fake_zenodo.py) running here
    sys.path.insert(0, str(BACKEND / "tests"))
    from fake_zenodo import FakeZenodo
    fake = FakeZenodo(f"http://127.0.0.1:{PORT + 2}")
    fake_server = fake.serve(PORT + 2)
    plain = {k: v for k, v in env.items() if k != "BENCHTOP_DEMO"} | {"BENCHTOP_ZENODO_URL": fake.base}
    try:
        more = serve(plain, data / "server-publishing.log", lambda: publishing(sync_playwright, fake))
    finally:
        fake_server.shutdown()
    problems += more if more is not None else [("publishing", "", "server", "Benchtop didn't start for the publishing check")]
    for p in problems:
        print("✗", " | ".join(p))
    print("Browser smoke test:", "passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def serve(env: dict, log_path: Path, run) -> list[tuple[str, str, str, str]] | None:
    """Start Benchtop with this environment, run the checks, stop it; any traceback in its log is a problem too.
    None: it didn't start."""
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
            return None
        problems = run()
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
        problems.append(("server", log_path.name, "traceback", f"{tracebacks} error(s) in the server log"))
        print(out[-4000:])
    return problems


def sweep(sync_playwright) -> list[tuple[str, str, str, str]]:
    tok = {u: httpx.post(B + "/api/auth/login", json={"username": u, "password": PW}).json()["token"] for u in ("slaurent", "hweiss")}
    H = {"Authorization": "Bearer " + tok["slaurent"]}
    exps = [e for e in httpx.get(B + "/api/experiments?limit=1000", headers=H).json()]
    mats = httpx.get(B + "/api/materials", headers=H).json()
    mats = mats["items"] if isinstance(mats, dict) else mats
    nbs = httpx.get(B + "/api/notebooks", headers=H).json()
    pages = (["/", "/guide", "/templates", "/materials", f"/materials/{mats[0]['id']}", "/inventory", "/search?q=suzuki", "/reactions",
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
    themed = ["/", f"/experiments/{exps[0]['id']}" if exps else "/", "/account", "/reactions"]
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
        problems += unsaved_typing(browser, tok["slaurent"])
        problems += demo_never_publishes(browser, tok["slaurent"])
        problems += getting_started(browser, tok["hweiss"])
        browser.close()
    return problems


def unsaved_typing(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.27: typing the server never got is kept in the browser and offered back when the record opens again.
    The server is "down" for saves (they're blocked), as when Benchtop stops or the Wi-Fi goes."""
    print("Unsaved typing: closing the tab while the server is down, a second tab, a record signed since…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []

    def bad(what: str) -> None:
        found.append(("unsaved typing", "", "draft", what))

    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Unsaved typing check"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Kept in the browser", "notebook_id": nb["id"]}).json()
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H,
               json={"kind": "text", "title": "Kept notes", "content": {"html": "<p>Started.</p>"}})
    entry = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Kept notes")
    box = f"#entry-{entry['id']} .ProseMirror"
    url = f"{B}/experiments/{exp['id']}"
    key = f"benchtop.draft.{httpx.get(B + '/api/auth/me', headers=H).json()['id']}.{entry['id']}"
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    down = lambda route: route.abort("internetdisconnected")  # noqa: E731

    def open_record():
        pg = ctx.new_page()
        pg.goto(url)
        pg.wait_for_selector(box)
        return pg

    def type_unsaved(pg, text: str) -> None:
        pg.route("**/api/entries/**", down)
        pg.click(box)
        pg.keyboard.press("End")
        pg.keyboard.type(text)
        pg.wait_for_selector(f"#entry-{entry['id']} .save-state.err", timeout=15000)  # "Not saved"
        pg.wait_for_timeout(600)

    def saved_html() -> str:
        e = httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()
        return next(x for x in e["entries"] if x["id"] == entry["id"])["content"]["html"]

    try:
        # 1. the tab is closed while the server is down; opened again later, the changes are offered and put back
        a = open_record()
        type_unsaved(a, " Colour change at 40 °C.")
        if not a.evaluate(f"localStorage.getItem('{key}')"):
            bad("nothing was kept in the browser while the section was unsaved")
        a.close(run_before_unload=False)
        b = open_record()
        try:
            b.wait_for_selector(f"#entry-{entry['id']} .draft-offer", timeout=5000)
        except Exception:  # noqa: BLE001
            bad("reopened, the record didn't offer the unsaved changes")
        else:
            if "Colour change" not in b.inner_text(f"#entry-{entry['id']} .draft-offer"):
                bad("the offer doesn't show what was typed")
            b.click("text=Put them back")
            state = f"document.querySelector('#entry-{entry['id']} .save-state').textContent"
            b.wait_for_function(f"/^v\\d+$/.test({state})", timeout=15000)
            if "Colour change at 40" not in saved_html():
                bad("put back, but the server doesn't have it")
            if b.evaluate(f"localStorage.getItem('{key}')"):
                bad("the kept copy stayed after the server had it")
        b.close()

        # 2. another tab of the same record doesn't offer changes a tab that's still open is holding
        a = open_record()
        type_unsaved(a, " Filtered.")
        b = open_record()
        b.wait_for_timeout(1500)
        if b.query_selector(".draft-offer"):
            bad("a second tab offered changes the first, still open, tab was holding")
        b.close()
        a.close(run_before_unload=False)

        # 3. signed since: the changes can't go back in; they can be copied, or discarded
        r = httpx.post(f"{B}/api/experiments/{exp['id']}/sign", headers=H, json={"password": PW})
        if r.status_code != 200:
            bad(f"couldn't sign the test record: {r.text[:100]}")
        c = open_record()
        try:
            c.wait_for_selector(".draft-offer", timeout=5000)
        except Exception:  # noqa: BLE001
            bad("a record signed since didn't mention the kept changes")
        else:
            if c.query_selector("text=Put them back") or not c.query_selector("text=Copy them"):
                bad("a signed record offered to put the changes back, or not to copy them")
            c.click("text=Discard")
            if c.query_selector(".draft-offer") or c.evaluate(f"localStorage.getItem('{key}')"):
                bad("Discard left the offer or the kept copy")
        c.close()
    finally:
        ctx.close()
    return found


def publishing(sync_playwright, fake) -> list[tuple[str, str, str, str]]:
    """0.28: an author connects a Zenodo token, looks at the preview, prepares a draft and publishes it; the record's page
    shows the DOI, and the record still matches its signatures. On a phone, the preview fits the screen."""
    print("Publishing on Zenodo: a token, the preview, a draft, publishing it (on a fake Zenodo)…", flush=True)
    from fake_zenodo import GOOD
    found: list[tuple[str, str, str, str]] = []

    def bad(what: str) -> None:
        found.append(("publishing", "", "zenodo", what))

    author = None
    for u in ("dcho", "pnair", "slaurent", "kwatanabe", "mruiz"):  # someone with witnessed records
        r = httpx.post(B + "/api/auth/login", json={"username": u, "password": PW})
        if r.status_code != 200:
            continue
        H = {"Authorization": "Bearer " + r.json()["token"]}
        me = httpx.get(B + "/api/auth/me", headers=H).json()
        mine = [e for e in httpx.get(B + "/api/experiments?limit=1000", headers=H).json()
                if e["owner"]["id"] == me["id"] and e["status"] == "witnessed"]
        if len(mine) >= 2:
            author = (r.json()["token"], H, mine)
            break
    if author is None:
        bad("the demo lab has nobody with two witnessed records to publish")
        return found
    token, H, mine = author
    exp, other = mine[0], mine[1]
    before = httpx.get(f"{B}/api/experiments/{exp['id']}/verify", headers=H).json()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, viewport in (("desktop", {"width": 1400, "height": 900}), ("phone", {"width": 390, "height": 844})):
            phone = viewport["width"] < 600
            ctx = browser.new_context(viewport=viewport, is_mobile=phone, has_touch=phone)
            ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
            pg = ctx.new_page()
            where = {"path": ""}

            def note(kind: str, text: str, label=label) -> None:
                if not any(i in text for i in IGNORE):
                    found.append((f"publishing {label}", where["path"], kind, text[:200]))
            pg.on("console", lambda m: m.type == "error" and note("console error", m.text))
            pg.on("pageerror", lambda e: note("page error", str(e)))
            pg.on("response", lambda r: "/api/" in r.url and (r.status >= 500 or r.status in (401, 403, 404, 422))
                  and note(f"HTTP {r.status}", r.url.replace(B, "")))

            def too_wide() -> None:
                over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if over > 2:
                    note("too wide", f"page is {over}px wider than the screen")
            try:
                if not phone:
                    where["path"] = "/account"
                    pg.goto(B + "/account")
                    pg.fill("input[aria-label='Zenodo personal access token']", GOOD)
                    pg.click("#zenodo button:has-text('Connect')")
                    pg.wait_for_selector("#zenodo >> text=Connected to", timeout=15000)
                target = other if phone else exp
                where["path"] = f"/experiments/{target['id']}"
                pg.goto(B + where["path"])
                pg.click(".publish-panel >> text=Publish on Zenodo…")
                pg.wait_for_selector("dialog.modal >> text=Files that become public", timeout=15000)
                pg.wait_for_timeout(300)
                too_wide()
                if phone:
                    ctx.close()
                    continue
                pg.click("dialog.modal >> text=Add an author")
                pg.fill("input[aria-label='Author 2: name']", "Ruiz, Maria")
                pg.click("dialog.modal button:has-text('Prepare on Zenodo')")
                pg.wait_for_selector(".publish-panel button:has-text('Publish…')", timeout=60000)  # the draft is there
                drafts = [d for d in fake.deps.values() if not d["submitted"]]
                if len(drafts) != 1 or len(fake.content[drafts[0]["id"]]) != 2 or fake.published():
                    bad("preparing didn't leave one unpublished draft holding the PDF and the .eln")
                elif [c["name"] for c in drafts[0]["metadata"]["creators"]][1:] != ["Ruiz, Maria"]:
                    bad("the co-author added in the preview didn't reach Zenodo")
                pg.click(".publish-panel button:has-text('Publish…')")
                pg.wait_for_selector("dialog.modal >> text=This can't be undone")
                if not pg.is_disabled("dialog.modal button:has-text('Publish for good')"):
                    bad("Publish could be clicked before confirming it can't be undone")
                pg.check("dialog.modal input[type=checkbox]")
                pg.click("dialog.modal button:has-text('Publish for good')")
                pg.wait_for_selector(".publish-panel .published-box", timeout=30000)
                doi = pg.inner_text(".doi-line") if pg.query_selector(".doi-line") else ""
                if len(fake.published()) != 1 or fake.published()[0]["doi"] not in doi:
                    bad(f"after publishing, the record's page doesn't show the DOI Zenodo registered ({doi!r})")
                after = httpx.get(f"{B}/api/experiments/{exp['id']}/verify", headers=H).json()
                if not after["intact"] or after["content_hash"] != before["content_hash"]:
                    bad("publishing changed what the signatures cover")
                pg.click("role=tab[name='Audit trail']")
                pg.wait_for_selector("text=published on", timeout=10000)
            except Exception as e:  # noqa: BLE001 - a step that didn't work is a problem to report, not a crash
                bad(f"{label}: {str(e).splitlines()[0][:160]}")
            ctx.close()
        browser.close()
    return found


def getting_started(browser, admin_token: str) -> list[tuple[str, str, str, str]]:
    """Someone signing in for the first time sees "New to Benchtop?" on the dashboard; it opens the guide, whose contents
    jump to each part, and once dismissed it stays away (it's saved to their account, so on every computer)."""
    print("Getting started: a brand-new account, the welcome card, the guide, dismissing the card…", flush=True)
    found: list[tuple[str, str, str, str]] = []
    where = {"path": "/"}

    def bad(kind: str, what: str) -> None:
        if not any(i in what for i in IGNORE):
            found.append(("getting started", where["path"], kind, what[:200]))
    password = "quartz-flask-ember-91"
    r = httpx.post(B + "/api/users", headers={"Authorization": "Bearer " + admin_token},
                   json={"username": "newcomer", "password": password, "full_name": "Nadia Newcomer", "role": "scientist", "must_change": False})
    if r.status_code != 201:
        return [("getting started", "", "account", f"couldn't make a new account: {r.text[:120]}")]
    token = httpx.post(B + "/api/auth/login", json={"username": "newcomer", "password": password}).json()["token"]
    H = {"Authorization": "Bearer " + token}
    for label, viewport in (("phone", {"width": 390, "height": 844}), ("desktop", {"width": 1300, "height": 900})):
        phone = viewport["width"] < 600
        ctx = browser.new_context(viewport=viewport, is_mobile=phone, has_touch=phone)
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        pg = ctx.new_page()
        pg.on("console", lambda m: m.type == "error" and bad("console error", m.text))
        pg.on("pageerror", lambda e: bad("page error", str(e)))
        pg.on("response", lambda r: "/api/" in r.url and (r.status >= 500 or r.status in (401, 403, 404, 422))
              and bad(f"HTTP {r.status}", r.url.replace(B, "")))
        try:
            where["path"] = "/"
            pg.goto(B + "/")
            pg.wait_for_selector(".welcome-note >> text=New to Benchtop?", timeout=15000)
            pg.click(".welcome-note >> text=Getting started")
            pg.wait_for_selector("h1:has-text('Getting started')", timeout=10000)
            where["path"] = "/guide"
            if not pg.url.endswith("/guide"):
                bad("guide", f"the card's link went to {pg.url}")
            if pg.locator(".guide-sec h2").count() < 10:
                bad("guide", "the guide has fewer than 10 parts")
            pg.click(".guide-toc >> text=Signing and witnessing")
            pg.wait_for_timeout(300)
            top = pg.evaluate("document.getElementById('signing').getBoundingClientRect().top")
            if not 0 <= top < 120:
                bad("guide", f"the contents didn't go to Signing and witnessing (it's {top:.0f}px down)")
            over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 2:
                bad("too wide", f"page is {over}px wider than the screen")
            if label == "phone":  # on the phone: the card and the guide fit; dismissing is checked on the desktop
                ctx.close()
                continue
            where["path"] = "/"
            pg.goto(B + "/")
            pg.click(".welcome-note >> text=Dismiss")
            pg.wait_for_selector(".welcome-note", state="detached", timeout=5000)
            pg.wait_for_timeout(500)
            pg.reload()
            pg.wait_for_selector(".page-title", timeout=15000)
            pg.wait_for_timeout(500)
            if pg.query_selector(".welcome-note"):
                bad("welcome card", "dismissed, it came back when the page was opened again")
            if httpx.get(B + "/api/auth/me", headers=H).json()["preferences"].get("guide_dismissed") is not True:
                bad("welcome card", "dismissing it wasn't saved to the account")
            if not pg.query_selector(".side-foot a[href='/guide']"):
                bad("guide", "the sidebar has no link to Getting started")
        except Exception as e:  # noqa: BLE001 - a step that didn't work is a problem to report, not a crash
            bad("guide", f"{label}: {str(e).splitlines()[0][:160]}")
        ctx.close()
    return found


def demo_never_publishes(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.28: in the demo lab the preview opens (to see what would go), but nothing can be sent to Zenodo."""
    H = {"Authorization": "Bearer " + token}
    me = httpx.get(B + "/api/auth/me", headers=H).json()
    mine = [e for e in httpx.get(B + "/api/experiments?limit=1000", headers=H).json()
            if e["owner"]["id"] == me["id"] and e["status"] in ("signed", "witnessed")]
    if not mine:
        return [("demo publishing", "", "zenodo", "no signed record to try")]
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    found = []
    try:
        pg.goto(f"{B}/experiments/{mine[0]['id']}")
        pg.click(".publish-panel >> text=Publish on Zenodo…")
        pg.wait_for_selector("dialog.modal >> text=demo lab", timeout=15000)
        if not pg.is_disabled("dialog.modal button:has-text('Prepare on Zenodo')"):
            found.append(("demo publishing", "", "zenodo", "the demo lab offered to send a record to Zenodo"))
    except Exception as e:  # noqa: BLE001
        found.append(("demo publishing", "", "zenodo", str(e).splitlines()[0][:160]))
    ctx.close()
    return found


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
