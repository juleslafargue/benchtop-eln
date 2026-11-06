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
import re
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
                # 0.31.1: the parts of a list row never cover each other (on a phone, the Materials pictures were wider
                # than their column and covered the start of each name, from 0.7.3 to 0.31.0)
                clash = pg.evaluate("""() => {
                    for (const row of document.querySelectorAll('.list-row')) {
                        const parts = [...row.children].map(k => [k, k.getBoundingClientRect()]).filter(([, r]) => r.width && r.height)
                        for (let i = 0; i < parts.length; i++) for (let j = i + 1; j < parts.length; j++) {
                            const [a, ra] = parts[i], [b, rb] = parts[j]
                            const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left)
                            const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top)
                            if (x > 1 && y > 1) return `"${(a.innerText || a.className).trim().slice(0, 40)}" and "${(b.innerText || b.className).trim().slice(0, 40)}" overlap by ${Math.round(x)}px`
                        }
                    }
                    return '' }""")
                if clash:
                    note("overlap", f"in a list row, {clash}")
                if label == "desktop" and (path.startswith("/experiments/") or path == "/reactions"):
                    # 0.28 showed "AE 139%" (a co-reactant marked as Reagent); records signed back then still hold it
                    ae = [float(x) for x in re.findall(r"(?:Atom economy|AE|RME)\s*(\d+(?:\.\d+)?)\s*%", pg.inner_text("body"))]
                    if any(x > 100 for x in ae):
                        note("atom economy", f"shows {max(ae)} %, which is impossible")
            ctx.close()
        problems += unsaved_typing(browser, tok["slaurent"])
        problems += reaction_amounts(browser, tok["slaurent"])
        problems += text_file_viewer(browser, tok["slaurent"])
        problems += mention_pick(browser, tok["hweiss"])
        problems += demo_never_publishes(browser, tok["slaurent"])
        problems += getting_started(browser, tok["hweiss"])
        problems += first_time_review(browser, tok["slaurent"])
        problems += notebook_delete_archive(browser, tok["slaurent"])
        problems += slow_network(browser, tok["slaurent"])
        problems += numbers_and_network(browser, tok["hweiss"])
        problems += polish(browser, tok["hweiss"])
        problems += cut_short_lists(browser, tok["hweiss"])
        problems += scanning(browser, tok["slaurent"])
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


def reaction_amounts(browser, token: str) -> list[tuple[str, str, str, str]]:
    """A number typed in the reaction table stays. 0.28 and before: type 1.2 in a row's Equiv and Tab on; the Mass cell
    got focus before the recalculated mass arrived, still showed the old mass, and leaving it saved that old mass as if
    typed, putting Equiv back to 1.1. The server's answer is held back here until the cursor is in the Mass cell; then
    the cell is left with Tab, with Enter, with a click elsewhere, and after typing the mass that was there back in."""
    print("Reaction table: typing an equivalent and moving on before the recalculated amounts arrive…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Reaction table check"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Esterification amounts", "notebook_id": nb["id"]}).json()
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.12},
            {"role": "reactant", "name": "Benzyl alcohol", "smiles": "OCc1ccccc1", "equiv": 1.1, "basis": "equiv"}]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "reaction", "title": "Amounts", "content": {"rows": rows}})
    entry = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Amounts")
    sec = f"#entry-{entry['id']}"
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    held: list = []
    hold = {"on": False}
    pg.route("**/api/chem/stoichiometry", lambda route: held.append(route) if hold["on"] else route.continue_())
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        # 0.29: each amount cell is named by its column and its row, for screen readers ("Mass (mg), Benzyl alcohol")
        col = {"equiv": "Equiv", "mmol": "mmol", "mass_mg": "Mass (mg)"}
        pg.wait_for_selector(f"{sec} input[aria-label='Equiv, Benzoic acid']")
        cell = lambda k: pg.locator(f"{sec} input[aria-label='{col[k]}, Benzyl alcohol']")  # noqa: E731 - the second reactant's row
        for equiv, leave in (("1.2", "Tab"), ("1.3", "Enter"), ("1.4", "click"), ("1.5", "retype")):
            old_mass, old_mmol = cell("mass_mg").input_value(), cell("mmol").input_value()
            hold["on"] = True
            cell("equiv").click()
            cell("equiv").fill(equiv)
            pg.keyboard.press("Tab")  # to mmol
            pg.keyboard.press("Tab")  # to Mass
            for _ in range(50):
                if held:
                    break
                pg.wait_for_timeout(100)
            hold["on"] = False
            if pg.evaluate("document.activeElement.getAttribute('aria-label')") != "Mass (mg), Benzyl alcohol":
                found.append(("reaction table", "", leave, "two Tabs from Equiv didn't reach the Mass cell"))
            if leave == "retype":  # typed in the cell, but the same mass as was there
                pg.keyboard.press("Control+A")
                pg.keyboard.type(old_mass)
            while held:
                held.pop().continue_()
            # the answer has arrived when the mmol cell (left alone) shows the new amount
            pg.wait_for_function(f"document.querySelector(\"{sec} input[aria-label='mmol, Benzyl alcohol']\").value !== {old_mmol!r}", timeout=10000)
            if leave != "retype":
                try:
                    pg.wait_for_function(f"document.activeElement.value !== {old_mass!r}", timeout=5000)
                except Exception:  # noqa: BLE001
                    found.append(("reaction table", "", leave, "the Mass cell with the cursor in it kept showing the old mass"))
            if leave in ("Tab", "retype"):
                pg.keyboard.press("Tab")
            elif leave == "Enter":
                pg.keyboard.press("Enter")
            else:
                pg.click(f"{sec} .rxn-scheme")
            pg.wait_for_timeout(1500)
            pg.wait_for_selector(f"{sec} .save-state:not(.dirty)", timeout=15000)
            shown = cell("equiv").input_value()
            saved = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"]
                         if e["id"] == entry["id"])["content"]["rows"][1]
            if shown != equiv or saved.get("equiv") != float(equiv) or saved.get("basis") != "equiv":
                found.append(("reaction table", "", leave, f"typed Equiv {equiv}, left the Mass cell ({leave}): the table shows "
                              f"{shown}, the record has {saved.get('equiv')} from {saved.get('basis')}"))
            if "basis" in (cell("mass_mg").get_attribute("class") or ""):
                found.append(("reaction table", "", leave, "the Mass cell was marked as typed in"))
        # and a mass that is typed, in a unit, is still saved
        cell("mass_mg").click()
        cell("mass_mg").fill("0.25 g")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(1500)
        pg.wait_for_selector(f"{sec} .save-state:not(.dirty)", timeout=15000)
        saved = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"]
                     if e["id"] == entry["id"])["content"]["rows"][1]
        if saved.get("mass_mg") != 250 or saved.get("basis") != "mass" or cell("mass_mg").input_value() != "250":
            found.append(("reaction table", "", "typed mass", f"typed 0.25 g in Mass: the record has {saved.get('mass_mg')} mg from {saved.get('basis')}"))
    except Exception as e:  # noqa: BLE001
        found.append(("reaction table", "", "error", str(e).splitlines()[0][:160]))
    ctx.close()
    return found


def text_file_viewer(browser, token: str) -> list[tuple[str, str, str, str]]:
    """"View spectrum" is offered for a .txt that holds a spectrum (two columns of numbers), not for one of notes:
    0.28 offered it for any .txt, which then failed with "Expected two numeric columns (x, y)"."""
    print("Files: View spectrum only for a .txt that is one…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Text files check"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Notes and a trace", "notebook_id": nb["id"]}).json()
    xy = "".join(f"{4000 - i * 10} {0.5 + (i % 7) / 10}\n" for i in range(300))
    ids = [httpx.post(f"{B}/api/experiments/{exp['id']}/attachments", headers=H, files={"file": (name, body.encode(), "text/plain")}).json()["id"]
           for name, body in (("weighing notes.txt", "Weighed 120 mg on balance 2.\nColour: pale yellow.\n"), ("ir-trace.txt", xy))]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "file", "title": "Files", "content": {"attachment_ids": ids}})
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.wait_for_selector("ul.files li:has-text('ir-trace.txt') button:has-text('View spectrum')", timeout=15000)
        pg.wait_for_load_state("networkidle")
        if pg.query_selector("ul.files li:has-text('weighing notes.txt') button:has-text('View spectrum')"):
            found.append(("files", "", "txt", "a .txt of notes offers View spectrum"))
        pg.click("ul.files li:has-text('ir-trace.txt') button:has-text('View spectrum')")
        pg.wait_for_selector("ul.files li:has-text('ir-trace.txt') canvas", timeout=15000)
    except Exception as e:  # noqa: BLE001
        found.append(("files", "", "error", str(e).splitlines()[0][:160]))
    ctx.close()
    return found


def mention_pick(browser, admin_token: str) -> list[tuple[str, str, str, str]]:
    """Typing @nn and Enter in a comment mentions Nadia Newbie (@nnewbie). 0.28 listed everyone whose name contained
    the letters, in username order, so Enter picked Hannah Weiss (Ha-nn-ah)."""
    print("Mentions: @nn picks the person whose username starts with it…", flush=True)
    H = {"Authorization": "Bearer " + admin_token}
    r = httpx.post(B + "/api/users", headers=H, json={"username": "nnewbie", "password": "quartz-flask-ember-92",
                                                    "full_name": "Nadia Newbie", "role": "scientist", "must_change": False})
    if r.status_code != 201:
        return [("mentions", "", "account", f"couldn't make the account: {r.text[:120]}")]
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Mentions check"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Who did the TLC?", "notebook_id": nb["id"]}).json()
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{admin_token}')")
    pg = ctx.new_page()
    found = []
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        box = pg.locator("textarea[aria-label='Ask a question or leave a note…']")
        box.click()
        box.type("Thanks @nn")
        pg.wait_for_selector(".mention-menu li.on", timeout=10000)
        first = pg.inner_text(".mention-menu li.on")
        pg.keyboard.press("Enter")
        if "@nnewbie" not in first or box.input_value() != "Thanks @nnewbie ":
            found.append(("mentions", "", "pick", f"@nn + Enter gave {box.input_value()!r} (highlighted: {first!r})"))
    except Exception as e:  # noqa: BLE001
        found.append(("mentions", "", "error", str(e).splitlines()[0][:160]))
    ctx.close()
    found += sample_code_lookup(browser, "nnewbie", "quartz-flask-ember-92")
    return found


def sample_code_lookup(browser, username: str, password: str) -> list[tuple[str, str, str, str]]:
    """The ⌘K box and Search find an experiment from the sample code on its vial (0.28 found nothing: codes weren't
    searched). Typed as on a vial label: nn-1."""
    print("Sample codes: ⌘K and Search find the experiment a code was handed to…", flush=True)
    token = httpx.post(B + "/api/auth/login", json={"username": username, "password": password}).json()["token"]
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    httpx.put(B + "/api/auth/me/sample-prefix", headers=H, json={"prefix": "NN"})
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "First year"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Recrystallisation of crude amide", "notebook_id": nb["id"]}).json()
    code = httpx.post(f"{B}/api/experiments/{exp['id']}/samples", headers=H, json={}).json().get("code")
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    try:
        pg.goto(B + "/")
        pg.wait_for_selector(".palette-trigger")
        pg.wait_for_load_state("networkidle")
        pg.click(".palette-trigger")
        pg.fill(".palette-input", "nn-1")
        pg.wait_for_timeout(500)
        top = pg.inner_text(".palette-list li[aria-selected='true']") if pg.query_selector(".palette-list li[aria-selected='true']") else ""
        if exp["eid"] not in top:
            found.append(("sample codes", "⌘K", "find", f"nn-1 ({code}) in ⌘K: the first result is {top!r}, not {exp['eid']}"))
        pg.goto(f"{B}/search?q={code}")
        pg.wait_for_selector(".search-results li, .empty-state", timeout=15000)
        first = pg.inner_text(".search-results li") if pg.query_selector(".search-results li") else ""
        if exp["eid"] not in first:
            found.append(("sample codes", "/search", "find", f"searching {code}: the first result is {first[:80]!r}, not {exp['eid']}"))
    except Exception as e:  # noqa: BLE001
        found.append(("sample codes", "", "error", str(e).splitlines()[0][:160]))
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


def first_time_review(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.29, from a first-time user's review: the keyboard and screen-reader fixes, the reaction table fitting the page,
    a returned record saying so, the sign dialog's reminder (which changes nothing that is signed), and typing a new
    password twice. Each check here failed on 0.28."""
    print("First-time review: focus, labels, Skip to content, the reaction table's width, the sign reminder…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    where = {"path": ""}

    def bad(kind: str, what: str) -> None:
        found.append(("first-time review", where["path"], kind, what[:200]))
    exps = {e["eid"]: e for e in httpx.get(B + "/api/experiments?limit=1000", headers=H).json()}
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Review checks"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Signed with empty sections", "notebook_id": nb["id"]}).json()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    try:
        # Skip to content: the first Tab stop, and it moves the focus to the page's content
        where["path"] = "/"
        pg.goto(B + "/")
        pg.wait_for_selector(".page-title")
        pg.keyboard.press("Tab")
        if pg.evaluate("document.activeElement.textContent") != "Skip to content":
            bad("skip link", "the first Tab stop isn't Skip to content")
        pg.keyboard.press("Enter")
        if pg.evaluate("document.activeElement.id") != "main":
            bad("skip link", "Skip to content didn't move the focus to the content")

        # every text box and amount cell of a record is named: a text section by its title, an amount by column and row
        where["path"] = f"/experiments/{exp['id']}"
        pg.goto(B + where["path"])
        pg.wait_for_selector(".ProseMirror")
        names = pg.evaluate("""() => [...document.querySelectorAll('.entry .ProseMirror')].map(e => e.getAttribute('aria-label'))""")
        if names[:2] != ["Aim", "Procedure"] or None in names:
            bad("labels", f"text sections are named {names}, not by their titles")

        # the sign dialog: the password has the focus; the reminder lists what's empty; signing still works and sends
        # only what it sent before; the record matches its signature; closing gives the focus back to the button
        pg.click("button:has-text('Sign as author')")
        pg.wait_for_selector("dialog.modal input[type=password]", timeout=10000)
        pg.wait_for_timeout(500)
        if pg.evaluate("document.activeElement.type") != "password":
            bad("sign dialog", "the focus isn't on the password")
        pg.keyboard.press("Escape")
        pg.wait_for_selector("dialog.modal", state="detached")
        if pg.evaluate("document.activeElement.textContent") != "Sign as author":
            bad("dialog focus", "closing a dialog didn't give the focus back to the button that opened it")
        sent: list = []
        pg.on("request", lambda r: r.url.endswith(f"/api/experiments/{exp['id']}/sign") and sent.append(r.post_data_json))
        pg.click("button:has-text('Sign as author')")
        pg.wait_for_selector("dialog.modal input[type=password]")
        pg.wait_for_timeout(800)
        reminder = pg.inner_text("dialog.modal .sign-reminder") if pg.query_selector("dialog.modal .sign-reminder") else ""
        for part in ("Aim", "Procedure", "Observations & results", "hazards not recorded"):
            if part not in reminder:
                bad("sign reminder", f"{part!r} isn't in {reminder!r}")
        pg.fill("dialog.modal input[type=password]", PW)
        pg.click("dialog.modal button:has-text('Sign and lock')")
        pg.wait_for_selector("dialog.modal", state="detached", timeout=15000)
        signed = httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()
        check = httpx.get(f"{B}/api/experiments/{exp['id']}/verify", headers=H).json()
        if signed["status"] != "signed":
            bad("sign reminder", "with empty sections, signing didn't go through")
        elif not check["intact"] or signed["signatures"][-1]["content_hash"] != check["content_hash"]:
            bad("sign reminder", "the signature doesn't cover the record as the server computes it")
        if not sent or set(sent[0]) != {"password", "comment"}:
            bad("sign reminder", f"signing sent {sorted(sent[0]) if sent else 'nothing'}, not just the password and comment")

        # a returned record says so where its state is shown, with the reason
        where["path"] = f"/experiments/{exps['EXP-00025']['id']}"
        pg.goto(B + where["path"])
        pg.wait_for_selector(".status-card")
        card = pg.inner_text(".status-card")
        if "Returned by" not in card or "13C" not in card:
            bad("returned", f"the status shows {card!r}, not who returned it and why")

        # a new password is typed twice; the Password label belongs to its box
        where["path"] = "/account"
        pg.goto(B + "/account")
        pg.click("button:has-text('Change password')")
        pg.wait_for_selector("dialog.modal input[autocomplete=current-password]")
        pg.fill("dialog.modal input[autocomplete=current-password]", PW)
        pg.get_by_label("New password", exact=True).fill("copper-lantern-meadow-42")
        pg.get_by_label("Repeat password", exact=True).fill("copper-lantern-meadow-24")
        pg.wait_for_timeout(800)
        if not pg.is_disabled("dialog.modal button.primary"):
            bad("repeat password", "two different new passwords could be saved")
        pg.get_by_label("Repeat password", exact=True).fill("copper-lantern-meadow-42")
        pg.wait_for_function("!document.querySelector('dialog.modal button.primary').disabled", timeout=5000)
        pg.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001 - a step that didn't work is a problem to report, not a crash
        bad("error", str(e).splitlines()[0][:160])
    ctx.close()

    # the reaction table fits beside the sidebar: Yield (and every column) without scrolling sideways, numbers not cut;
    # in the default theme and in High contrast, whose typeface is wider
    for palette, width in [(p, w) for p in ("benchtop", "contrast") for w in (1280, 1400, 1920)]:
        httpx.patch(B + "/api/auth/me", headers=H, json={"preferences": {"palette": palette}})
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        pg = ctx.new_page()
        for eid in ("EXP-00033", "EXP-00018", "EXP-00015"):
            if eid not in exps:
                continue
            where["path"] = f"/experiments/{exps[eid]['id']} at {width} px ({palette})"
            try:
                pg.goto(f"{B}/experiments/{exps[eid]['id']}")
                pg.wait_for_selector("table.grid td.yield", timeout=15000)
                pg.wait_for_timeout(300)
                out = pg.evaluate("""() => [...document.querySelectorAll('.grid-wrap')].filter(w => w.querySelector('td.yield, input.lim-dot')).flatMap(w => {
                    const box = w.getBoundingClientRect(), why = []
                    if (w.scrollWidth > w.clientWidth + 1) why.push(`the table needs ${w.scrollWidth - w.clientWidth}px more`)
                    for (const y of w.querySelectorAll('td.yield')) if (y.getBoundingClientRect().right > box.right + 1) why.push('Yield is off to the side')
                    for (const i of w.querySelectorAll('input:not(.text)')) if (i.scrollWidth > i.clientWidth + 1) why.push(`${i.getAttribute('aria-label')} shows ${i.value} cut`)
                    return why })""")
                if out:
                    bad("reaction table", "; ".join(out[:3]))
                if width >= 1400 and eid == "EXP-00033" and palette == "benchtop":
                    # the names get the table's spare width (0.29.0 at first cut "Sodium L-asc…" to give numbers room)
                    # at 1400 px these two whole; at 1920 every name, the azide solution's and the triazole's too
                    cut = pg.evaluate(f"""() => [...document.querySelectorAll('table.grid input.text')]
                        .filter(i => ({width} >= 1920 || ['Sodium L-ascorbate', 'Phenylacetylene'].includes(i.value)) && i.scrollWidth > i.clientWidth + 1).map(i => i.value)""")
                    if cut:
                        bad("reaction table", f"names cut off: {', '.join(cut)}")
            except Exception as e:  # noqa: BLE001
                bad("reaction table", str(e).splitlines()[0][:160])
        ctx.close()
    httpx.patch(B + "/api/auth/me", headers=H, json={"preferences": {"palette": "benchtop"}})
    return found


def notebook_delete_archive(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.30: an empty notebook is deleted from its page (after a confirm) and is gone; one with an experiment can't be
    deleted, only archived: it leaves the sidebar's list, is found under Archived, says so on its page, takes no new
    experiments, and Unarchive brings it back."""
    print("Notebooks: deleting an empty one, archiving and unarchiving one with an experiment…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    where = {"path": ""}

    def bad(kind: str, what: str) -> None:
        found.append(("notebooks", where["path"], kind, what[:200]))
    empty = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Scratch notebook to delete"}).json()
    full = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Finished project to archive"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Kept after archiving", "notebook_id": full["id"]}).json()
    main_list = "() => [...document.querySelectorAll('#side-notebooks > a.nb-link .nb-name')].map(a => a.textContent)"
    archived_list = "() => [...document.querySelectorAll('#side-archived a.nb-link .nb-name')].map(a => a.textContent)"
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    try:
        # delete the empty one: confirm, back on the dashboard, gone from the sidebar and the server
        where["path"] = f"/notebooks/{empty['id']}"
        pg.goto(B + where["path"])
        pg.wait_for_selector("button:has-text('Delete notebook')", timeout=15000)
        if pg.query_selector("button:has-text('Archive notebook')"):
            bad("buttons", "an empty notebook offers Archive as well as Delete")
        pg.click("button:has-text('Delete notebook')")
        pg.wait_for_selector("dialog.modal >> text=Scratch notebook to delete")
        pg.click("dialog.modal button:has-text('Delete notebook')")
        pg.wait_for_url(B + "/", timeout=15000)
        pg.wait_for_timeout(500)
        if "Scratch notebook to delete" in pg.evaluate(main_list):
            bad("delete", "the deleted notebook is still in the sidebar")
        if any(n["id"] == empty["id"] for n in httpx.get(B + "/api/notebooks?include_archived=true", headers=H).json()):
            bad("delete", "the server still has the notebook")

        # one with an experiment: no Delete, Archive (with a confirm) instead
        where["path"] = f"/notebooks/{full['id']}"
        pg.goto(B + where["path"])
        pg.wait_for_selector("button:has-text('Archive notebook')", timeout=15000)
        if pg.query_selector("button:has-text('Delete notebook')"):
            bad("buttons", "a notebook with an experiment offers Delete")
        pg.click("button:has-text('Archive notebook')")
        pg.wait_for_selector("dialog.modal >> text=No new experiments")
        pg.click("dialog.modal button:has-text('Archive notebook')")
        pg.wait_for_selector(".archived-note", timeout=15000)
        if pg.query_selector(".page button:has-text('New experiment')"):
            bad("archived page", "an archived notebook's page offers New experiment")
        # the sidebar: out of the main list, in the Archived list
        where["path"] = "/"
        pg.goto(B + "/")
        pg.wait_for_selector(".archived-toggle", timeout=15000)
        if "Finished project to archive" in pg.evaluate(main_list):
            bad("archive", "the archived notebook is still in the sidebar's main list")
        pg.click(".archived-toggle")
        if "Finished project to archive" not in pg.evaluate(archived_list):
            bad("archive", "the archived notebook isn't under Archived")
        pg.click("#side-archived a.nb-link:has-text('Finished project to archive')")
        pg.wait_for_selector(".archived-note >> text=This notebook is archived.", timeout=15000)
        # its experiment still opens, says where it is, and offers no copy into the archived notebook
        where["path"] = f"/experiments/{exp['id']}"
        pg.goto(B + where["path"])
        pg.wait_for_selector(".exp-head >> text=(archived)", timeout=15000)
        if pg.query_selector("button:has-text('Duplicate')"):
            bad("archived record", "Duplicate is offered, but the copy can't go into an archived notebook")
        # Unarchive brings it back
        where["path"] = f"/notebooks/{full['id']}"
        pg.goto(B + where["path"])
        pg.click(".archived-note button:has-text('Unarchive')")
        pg.wait_for_selector(".archived-note", state="detached", timeout=15000)
        pg.wait_for_selector(".page button:has-text('New experiment')", timeout=15000)
        if "Finished project to archive" not in pg.evaluate(main_list):
            bad("unarchive", "the notebook isn't back in the sidebar's list")
    except Exception as e:  # noqa: BLE001
        bad("error", str(e).splitlines()[0][:160])
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


class Slow:
    """Holds the page's requests to `pattern` (of `method`) for `delay` seconds, as a slow network would; they go on
    while the test waits through pump()."""

    def __init__(self, pg, pattern: str, delay: float, method: str | None = None):
        self.pg, self.delay, self.method, self.held, self.seen = pg, delay, method, [], 0
        pg.route(pattern, self._hold)

    def _hold(self, route) -> None:
        if self.method and route.request.method != self.method:
            route.continue_()
            return
        self.seen += 1
        self.held.append((time.time(), route))

    def pump(self, seconds: float = 0.0, until=None) -> bool:
        """Let held requests go once they've waited long enough, for `seconds`, or until `until()` is true."""
        end = time.time() + seconds
        while True:
            for item in list(self.held):
                if time.time() - item[0] >= self.delay:
                    self.held.remove(item)
                    item[1].continue_()
            if until is not None and until():
                return True
            if time.time() >= end:
                return until is None
            self.pg.wait_for_timeout(50)

    def release(self) -> None:
        """Let everything still held go (before the page closes)."""
        for _, route in self.held:
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - the page may be gone already
                pass
        self.held.clear()


def slow_network(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.30.1, on a slow network (found by the 0.30 stress test):
    - a recalculation of the reaction table that comes back late doesn't wipe a name, a note or the conditions typed
      while it was calculating;
    - typing while a save is on its way never makes the section's own saves collide ("changed elsewhere");
    - Sign as author saves every section first ("Saving…"), so the signature covers the last typing, and the
      reminder of empty sections is worked out from what's saved."""
    print("Slow network: a late reaction recalculation, overlapping saves, signing straight after typing…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Slow network check"}).json()

    def stored(exp_id: int, entry_id: int) -> dict:
        e = httpx.get(f"{B}/api/experiments/{exp_id}", headers=H).json()
        return next(x for x in e["entries"] if x["id"] == entry_id)

    def page():
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        pg = ctx.new_page()
        conflicts: list[str] = []
        pg.on("response", lambda r: r.status == 409 and "/api/entries/" in r.url and conflicts.append(r.url))
        return ctx, pg, conflicts

    saved = lambda pg, sec: bool(re.fullmatch(r"v\d+", pg.inner_text(f"{sec} .save-state").strip()))  # noqa: E731

    # 1. the reaction table
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Late recalculation", "notebook_id": nb["id"]}).json()
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.12},
            {"role": "reactant", "name": "Benzyl alcohol", "smiles": "OCc1ccccc1", "equiv": 1.1, "basis": "equiv"}]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "reaction", "title": "Amounts", "content": {"rows": rows}})
    entry = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Amounts")
    sec = f"#entry-{entry['id']}"
    ctx, pg, _ = page()
    slow = Slow(pg, "**/api/chem/stoichiometry", 2.5)
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.wait_for_selector(f"{sec} input[aria-label='Equiv, Benzyl alcohol']")
        pg.fill(f"{sec} input[aria-label='Equiv, Benzyl alcohol']", "1.3")
        pg.keyboard.press("Tab")
        name = pg.locator(f"{sec} input[aria-label='Name']").nth(1)
        name.click()
        name.press("End")
        pg.keyboard.type(" (dry)", delay=30)
        notes = pg.locator(f"{sec} .conditions input[placeholder^='e.g. degassed']")
        notes.click()
        pg.keyboard.type("degassed with argon", delay=30)
        pg.select_option(f"{sec} .conditions select", "Ar")
        slow.pump(20, until=lambda: slow.seen and not slow.held and saved(pg, sec))
        slow.pump(3.0)  # a second recalculation, if one was needed
        slow.pump(20, until=lambda: not slow.held and saved(pg, sec))
        c = stored(exp["id"], entry["id"])["content"]
        shown = (name.input_value(), notes.input_value(), pg.input_value(f"{sec} .conditions select"))
        if shown != ("Benzyl alcohol (dry)", "degassed with argon", "Ar"):
            found.append(("slow network", "", "reaction", f"typed while it was calculating, the section now shows {shown}"))
        have = ([r.get("name") for r in c["rows"]], (c.get("conditions") or {}).get("notes"), (c.get("conditions") or {}).get("atmosphere"))
        if "Benzyl alcohol (dry)" not in have[0] or have[1:] != ("degassed with argon", "Ar"):
            found.append(("slow network", "", "reaction", f"typed while it was calculating, the record has {have}"))
        if next(r for r in c["rows"] if r["smiles"] == "OCc1ccccc1").get("equiv") != 1.3:
            found.append(("slow network", "", "reaction", "the equivalents typed weren't kept"))
    except Exception as e:  # noqa: BLE001
        found.append(("slow network", "", "reaction", str(e).splitlines()[0][:160]))
    slow.release()
    ctx.close()

    # 2. typing in bursts while each save takes 2.5 s
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Overlapping saves", "notebook_id": nb["id"]}).json()
    aim = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Aim")
    sec = f"#entry-{aim['id']}"
    ctx, pg, conflicts = page()
    slow = Slow(pg, "**/api/entries/**", 2.5, "PUT")
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.click(f"{sec} .ProseMirror")
        for part in ("First part", ", second part", ", third."):
            pg.keyboard.type(part, delay=20)
            slow.pump(1.2)
        slow.pump(20, until=lambda: not slow.held and saved(pg, sec))
        html = stored(exp["id"], aim["id"])["content"]["html"]
        if conflicts or "First part, second part, third." not in html or not saved(pg, sec):
            found.append(("slow network", "", "saves", f"{len(conflicts)} save(s) collided with this tab's own; the section says "
                          f"{pg.inner_text(sec + ' .save-state')!r}, the record has {html[:80]!r}"))
    except Exception as e:  # noqa: BLE001
        found.append(("slow network", "", "saves", str(e).splitlines()[0][:160]))
    slow.release()
    ctx.close()

    # 3. signing straight after typing, while the save is on its way
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Signed straight after typing", "notebook_id": nb["id"]}).json()
    aim = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Aim")
    sec = f"#entry-{aim['id']}"
    ctx, pg, _ = page()
    slow = Slow(pg, "**/api/entries/**", 3.0, "PUT")
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.click(f"{sec} .ProseMirror")
        pg.keyboard.type("Make the triazole in one pot.", delay=10)
        pg.click("button:has-text('Sign as author')")
        if pg.query_selector("dialog[open]") or not pg.query_selector(".rail button:has-text('Saving…')"):
            found.append(("slow network", "", "signing", "the sign dialog opened before the sections were saved (no “Saving…”)"))
        slow.pump(15, until=lambda: bool(pg.query_selector("dialog[open]")))
        slow.pump(1.0)
        reminder = " ".join(pg.locator("dialog[open] .sign-reminder").all_inner_texts())
        if "Aim" in reminder:
            found.append(("slow network", "", "signing", f"the reminder lists a section that was just filled in: {reminder[:120]}"))
        pg.keyboard.type(PW)
        pg.keyboard.press("Enter")
        slow.pump(10, until=lambda: httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["status"] == "signed")
        slow.pump(1.0)
        html = stored(exp["id"], aim["id"])["content"]["html"]
        check = httpx.get(f"{B}/api/experiments/{exp['id']}/verify", headers=H).json()
        if "Make the triazole in one pot." not in html or not check["intact"]:
            found.append(("slow network", "", "signing", f"signed straight after typing: the signed record has {html[:80]!r}"))
    except Exception as e:  # noqa: BLE001
        found.append(("slow network", "", "signing", str(e).splitlines()[0][:160]))
    slow.release()
    ctx.close()
    return found


def numbers_and_network(browser, admin_token: str) -> list[tuple[str, str, str, str]]:
    """0.30.1, wrong numbers and error pages (found by the 0.30 stress test), as a new scientist sees them:
    - "1,000" in a mass box isn't taken as 1 mg: it stays, with a short message, and nothing is saved;
    - the dashboard says how many there are in all ("12 open"), and its lists end "and 2 more";
    - Create experiment / Create notebook make one each however often they're clicked ("Creating…"), and Enter in the
      new experiment's title creates it;
    - one failed request at start-up doesn't sign the person out: it says Benchtop can't be reached, and tries again;
    - a section that couldn't be saved says Benchtop can't be reached (not "Failed to fetch"), and saves by itself
      when it can (Ctrl+S tries at once, and so does coming back online);
    - a notebook whose experiments couldn't be loaded says so, with Retry (not "No experiments here yet"), and
      deleting one is reported as done even if the refreshed list fails to load."""
    print("Numbers and network trouble: “1,000”, dashboard totals, double clicks, start-up and saving offline…", flush=True)
    A = {"Authorization": "Bearer " + admin_token}
    found: list[tuple[str, str, str, str]] = []

    def bad(kind: str, what: str) -> None:
        found.append(("numbers & network", kind, "", what[:200]))
    r = httpx.post(B + "/api/users", headers=A, json={"username": "nnumbers", "password": "a-long-smoke-password", "must_change": False,
                                                       "full_name": "Nora Numbers", "role": "scientist"})
    token = httpx.post(B + "/api/auth/login", json={"username": "nnumbers", "password": "a-long-smoke-password"}).json()["token"]
    H = {"Authorization": "Bearer " + token}
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Nora's notebook"}).json()
    httpx.patch(B + "/api/auth/me", headers=H, json={"preferences": {"guide_dismissed": True, "default_notebook_id": nb["id"]}})
    exps = [httpx.post(B + "/api/experiments", headers=H, json={"title": f"Draft {i}", "notebook_id": nb["id"]}).json() for i in range(12)]
    exp = exps[0]
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.12}]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "reaction", "title": "Amounts", "content": {"rows": rows}})
    entries = httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"]
    rxn = next(e for e in entries if e["title"] == "Amounts")
    aim = next(e for e in entries if e["title"] == "Aim")

    def count(path: str, title: str, key: str = "title") -> int:
        return sum(1 for x in httpx.get(B + path, headers=H).json() if x[key] == title)

    def page(**kw):
        ctx = browser.new_context(viewport={"width": 1300, "height": 900}, **kw)
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        return ctx, ctx.new_page()

    # "1,000" in a mass box
    ctx, pg = page()
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        box = f"#entry-{rxn['id']} input[aria-label='Mass (mg), Benzoic acid']"
        pg.wait_for_selector(box)
        pg.fill(box, "1,000")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(2500)
        stored = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == rxn["id"])
        mass = stored["content"]["rows"][0]["mass_mg"]
        msg = " ".join(pg.locator(f"#entry-{rxn['id']} .num-problem-line").all_inner_texts())
        if mass != 122.12 or pg.input_value(box) != "1,000" or "1000 or 1?" not in msg:
            bad("1,000", f"typed 1,000 in a mass box: the record holds {mass} mg, the box shows {pg.input_value(box)!r}, it says {msg!r}")
    except Exception as e:  # noqa: BLE001
        bad("1,000", str(e).splitlines()[0][:160])
    ctx.close()

    # the dashboard's totals; double clicks and Enter in the New experiment and New notebook dialogs (on a slow network)
    ctx, pg = page()
    try:
        pg.goto(B + "/")
        pg.wait_for_selector(".lede", timeout=15000)
        pg.wait_for_selector(".w-list", timeout=15000)
        lede, more = pg.inner_text(".lede"), " ".join(pg.locator(".w-more").all_inner_texts())
        if "12 open" not in lede or "and 2 more" not in more:
            bad("dashboard", f"12 open experiments: the dashboard says {lede!r}, and its list ends {more!r} (not “and 2 more”)")
    except Exception as e:  # noqa: BLE001
        bad("dashboard", str(e).splitlines()[0][:160])
    try:
        slow = Slow(pg, "**/api/experiments", 2.0, "POST")
        pg.keyboard.press("n")
        pg.fill("dialog[open] input.input >> nth=0", "Clicked twice")
        pg.dblclick("dialog[open] button.primary")
        label = pg.inner_text("dialog[open] button.primary")
        if label != "Creating…" or not pg.is_disabled("dialog[open] button.primary"):
            bad("double click", f"while the experiment is being made, Create says {label!r}")
        slow.pump(10, until=lambda: "/experiments/" in pg.url)
        slow.pump(1.0)
        if count("/api/experiments?limit=500", "Clicked twice") != 1:
            bad("double click", f"a double click on Create experiment made {count('/api/experiments?limit=500', 'Clicked twice')} experiments")
        pg.goto(B + "/")
        pg.wait_for_selector(".lede")
        pg.keyboard.press("n")
        pg.fill("dialog[open] input.input >> nth=0", "Made with Enter")
        pg.keyboard.press("Enter")
        slow.pump(8, until=lambda: "/experiments/" in pg.url)
        if count("/api/experiments?limit=500", "Made with Enter") != 1:
            bad("enter", "Enter in the new experiment's title didn't create it")
        slow.release()
        pg.goto(B + "/")
        slow = Slow(pg, "**/api/notebooks", 2.0, "POST")
        pg.click(".w-actions button:has-text('New notebook')")
        pg.fill("dialog[open] input.input >> nth=0", "Notebook clicked twice")
        pg.dblclick("dialog[open] button.primary")
        slow.pump(10, until=lambda: "/notebooks/" in pg.url)
        slow.pump(1.0)
        n = count("/api/notebooks", "Notebook clicked twice", "name")
        if n != 1:
            bad("double click", f"a double click on Create notebook made {n} notebooks")
        slow.release()
    except Exception as e:  # noqa: BLE001
        bad("dashboard & dialogs", str(e).splitlines()[0][:160])
    ctx.close()

    # one failed request at start-up
    ctx, pg = page()
    try:
        failed = {"n": 0}

        def once(route):
            if failed["n"] == 0:
                failed["n"] += 1
                return route.abort("internetdisconnected")
            return route.continue_()
        pg.route("**/api/auth/me", once)
        pg.goto(B + "/")
        pg.wait_for_selector(".boot-problem >> text=Can't reach Benchtop", timeout=5000)
        if not pg.evaluate("localStorage.getItem('benchtop.token')"):
            bad("start-up", "one failed request at start-up signed the person out")
        pg.wait_for_selector(".lede", timeout=15000)  # it tried again by itself, and got in
    except Exception as e:  # noqa: BLE001
        bad("start-up", f"one failed request at start-up: {str(e).splitlines()[0][:120]}; the page says {pg.inner_text('body')[:80]!r}")
    ctx.close()

    # saving while Benchtop can't be reached
    ctx, pg = page()
    sec = f"#entry-{aim['id']}"
    state = lambda: pg.inner_text(f"{sec} .save-state").strip()  # noqa: E731
    down = lambda route: route.abort("internetdisconnected") if route.request.method == "PUT" else route.continue_()  # noqa: E731
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.wait_for_selector(f"{sec} .ProseMirror")
        pg.route("**/api/entries/**", down)
        pg.click(f"{sec} .ProseMirror")
        pg.keyboard.type("Typed offline.", delay=10)
        pg.wait_for_selector(f"{sec} .save-state.err", timeout=8000)
        said = pg.inner_text(f"{sec} p.error")
        if "Can't reach Benchtop" not in said:
            bad("saving offline", f"a section that couldn't be saved says {said!r}")
        pg.unroute("**/api/entries/**")
        pg.keyboard.press("Control+s")
        pg.wait_for_timeout(1500)
        if not re.fullmatch(r"v\d+", state()):
            bad("saving offline", f"Ctrl+S didn't try the failed save again (the section says {state()!r})")
        pg.wait_for_function(f"/^v\\d+$/.test(document.querySelector('{sec} .save-state').textContent.trim())", timeout=8000)
        pg.route("**/api/entries/**", down)
        pg.keyboard.type(" And again.", delay=10)
        pg.wait_for_selector(f"{sec} .save-state.err", timeout=8000)
        pg.unroute("**/api/entries/**")
        try:  # nothing pressed: it tries again by itself
            pg.wait_for_function(f"/^v\\d+$/.test(document.querySelector('{sec} .save-state').textContent.trim())", timeout=9000)
        except Exception:  # noqa: BLE001
            bad("saving offline", f"once Benchtop could be reached again, the section still says {state()!r}")
        ctx.set_offline(True)
        pg.keyboard.type(" Third.", delay=10)
        pg.wait_for_selector(f"{sec} .save-state.err", timeout=8000)
        ctx.set_offline(False)
        pg.wait_for_timeout(1500)
        if not re.fullmatch(r"v\d+", state()):
            bad("saving offline", f"back online, the section wasn't saved at once (it says {state()!r})")
        html = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == aim["id"])["content"]["html"]
        if "Typed offline. And again. Third." not in html:
            bad("saving offline", f"the record has {html[:80]!r}")
    except Exception as e:  # noqa: BLE001
        bad("saving offline", str(e).splitlines()[0][:160])
    ctx.close()

    # a notebook page whose list fails to load; deleting a notebook while the network drops
    ctx, pg = page()
    try:
        listing = lambda url: "/api/experiments?notebook_id=" in url  # noqa: E731
        pg.route(listing, lambda route: route.fulfill(status=500, body='{"detail": "Database busy"}', content_type="application/json"))
        pg.goto(f"{B}/notebooks/{nb['id']}")
        pg.wait_for_timeout(2000)
        text = pg.inner_text("main")
        if "Couldn't load the experiments" not in text or "No experiments here yet" in text:
            bad("notebook page", "its experiments couldn't be loaded, and the page says " + ("“No experiments here yet”" if "No experiments here yet" in text else "nothing"))
        pg.unroute(listing)
        pg.click("main button:has-text('Retry')", timeout=5000)
        pg.wait_for_selector("main >> text=Draft 11", timeout=10000)
    except Exception as e:  # noqa: BLE001
        bad("notebook page", str(e).splitlines()[0][:160])
    try:
        empty = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Deleted while the network drops"}).json()
        pg.goto(f"{B}/notebooks/{empty['id']}")
        pg.click("button:has-text('Delete notebook')")
        pg.wait_for_selector("dialog.modal >> text=Deleted while the network drops")
        pg.route(lambda url: "/api/notebooks?include_archived=true" in url, lambda route: route.abort("internetdisconnected"))
        pg.click("dialog.modal button:has-text('Delete notebook')")
        pg.wait_for_selector(".toast", timeout=10000)
        toast = pg.inner_text(".toast")
        if not toast.startswith("Deleted Deleted while the network drops"):
            bad("notebook delete", f"deleted, but the list didn't load after it: the page says {toast!r}")
    except Exception as e:  # noqa: BLE001
        bad("notebook delete", str(e).splitlines()[0][:160])
    ctx.close()
    return found


def polish(browser, admin_token: str) -> list[tuple[str, str, str, str]]:
    """0.30.1, the smaller things (found by the 0.30 stress test), as a new scientist sees them:
    - a number box keeps what it can't read ("12 mL" in a mass box), says why, and saves nothing; Escape puts back
      what was there; "25 °C" in Temperature is 25;
    - an error under the reaction table goes once a later recalculation works;
    - big amounts (123457 mg) and tiny equivalents (0.000123) are shown whole, and Yield stays in view at 1280 px;
    - HRMS: Use doesn't move as the ppm appears; the Reactions page's yield filter works without Enter; Record change
      can be pressed as soon as an amount is typed;
    - Admin: an arrow key on a role menu asks first, it doesn't change the role;
    - N pressed before the notebooks have loaded still picks the default notebook;
    - a notebook deleted in another tab says so, with the way back to the dashboard;
    - ⌘K finds a sample code it hasn't loaded, by asking Benchtop."""
    print("Polish: number boxes, big amounts, HRMS, filters, role changes, deleted notebooks, ⌘K codes…", flush=True)
    A = {"Authorization": "Bearer " + admin_token}
    found: list[tuple[str, str, str, str]] = []

    def bad(kind: str, what: str) -> None:
        found.append(("polish", kind, "", what[:220]))
    me = httpx.post(B + "/api/users", headers=A, json={"username": "ppolish", "password": "a-long-smoke-password", "must_change": False,
                                                       "full_name": "Pat Polish", "role": "scientist"}).json()
    token = httpx.post(B + "/api/auth/login", json={"username": "ppolish", "password": "a-long-smoke-password"}).json()["token"]
    H = {"Authorization": "Bearer " + token}
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Pat's notebook"}).json()
    httpx.patch(B + "/api/auth/me", headers=H, json={"preferences": {"guide_dismissed": True, "default_notebook_id": nb["id"]}})
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Polish checks", "notebook_id": nb["id"]}).json()
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.12},
            {"role": "product", "name": "Benzamide", "smiles": "NC(=O)c1ccccc1", "equiv": 1}]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "reaction", "title": "Amounts", "content": {"rows": rows}})
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "characterization", "title": "HRMS check",
                                                                           "content": {"items": [{"technique": "HRMS", "conditions": "", "data": ""}]}})
    big = httpx.post(B + "/api/experiments", headers=H, json={"title": "Big amounts", "notebook_id": nb["id"]}).json()
    big_rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 123456.789, "purity": 99.987},
                {"role": "reagent", "name": "Catalyst", "smiles": "CC(=O)O", "basis": "equiv", "equiv": 0.000123, "density": 1.04912},
                {"role": "solvent", "name": "DMF", "smiles": "CN(C)C=O", "basis": "volume", "volume_ml": 1234.5678, "density": 0.944},
                {"role": "product", "name": "Benzamide", "smiles": "NC(=O)c1ccccc1", "equiv": 1, "actual_mass_mg": 98765.4321, "purity": 99.5}]
    httpx.post(f"{B}/api/experiments/{big['id']}/entries", headers=H, json={"kind": "reaction", "title": "Big", "content": {"rows": big_rows}})
    entries = httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"]
    rxn = next(e for e in entries if e["title"] == "Amounts")
    hrms = next(e for e in entries if e["title"] == "HRMS check")
    stored = lambda: next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == rxn["id"])["content"]  # noqa: E731

    def page(width=1300, **kw):
        ctx = browser.new_context(viewport={"width": width, "height": 900}, **kw)
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        return ctx, ctx.new_page()

    # a number box: what it can't read stays, with why; Escape puts back what was there; °C is read
    ctx, pg = page()
    sec = f"#entry-{rxn['id']}"
    box = f"{sec} input[aria-label='Mass (mg), Benzoic acid']"
    try:
        pg.goto(f"{B}/experiments/{exp['id']}")
        pg.wait_for_selector(box)
        pg.fill(box, "12 mL")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(1500)
        msg = " ".join(pg.locator(f"{sec} .num-problem-line").all_inner_texts())
        mass = stored()["rows"][0]["mass_mg"]
        if pg.input_value(box) != "12 mL" or "can't be turned into mg" not in msg or mass != 122.12:
            bad("number box", f"typed 12 mL in a mass box: it shows {pg.input_value(box)!r}, says {msg!r}, the record has {mass} mg")
        pg.click(box)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        if pg.input_value(box) != "122.12" or pg.locator(f"{sec} .num-problem-line").count():
            bad("Escape", f"Escape in the mass box left {pg.input_value(box)!r} (not 122.12) and {pg.locator(f'{sec} .num-problem-line').count()} message(s)")
        pg.click(box)
        pg.keyboard.press("Control+A")
        pg.keyboard.type("200")
        pg.keyboard.press("Escape")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(1500)
        if pg.input_value(box) != "122.12" or stored()["rows"][0]["mass_mg"] != 122.12:
            bad("Escape", f"typed 200 then Escape: the box shows {pg.input_value(box)!r}, the record has {stored()['rows'][0]['mass_mg']} mg")
        temp = f"{sec} input[aria-label='Temperature in °C']"
        pg.fill(temp, "25 °C")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(2000)
        if (stored().get("conditions") or {}).get("temperature_c") != 25:
            bad("temperature", f"typed 25 °C: the record has {(stored().get('conditions') or {}).get('temperature_c')!r}")
        # an error from a recalculation that failed goes once one works
        failing = {"n": 1}

        def once(route):
            if failing["n"]:
                failing["n"] -= 1
                return route.fulfill(status=500, body='{"detail": "Calculation failed"}', content_type="application/json")
            return route.continue_()
        pg.route("**/api/chem/stoichiometry", once)
        eq = f"{sec} input[aria-label='Equiv, Benzamide']"
        pg.fill(eq, "0.9")
        pg.keyboard.press("Tab")
        pg.wait_for_selector(f"{sec} p.error", timeout=8000)
        pg.fill(eq, "1")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(2000)
        if pg.locator(f"{sec} p.error").count():
            bad("old error", f"after a recalculation that worked, the table still says {pg.inner_text(f'{sec} p.error')!r}")
        pg.unroute("**/api/chem/stoichiometry")
    except Exception as e:  # noqa: BLE001
        bad("number box", str(e).splitlines()[0][:160])
    # HRMS: Use stays where it is while the found m/z is typed
    try:
        hs = f"#entry-{hrms['id']}"
        found_box = pg.locator(f"{hs} input[aria-label='Found m/z']")
        found_box.scroll_into_view_if_needed()
        use = pg.locator(f"{hs} button:has-text('Use')")
        x0 = use.bounding_box()["x"]
        found_box.click()
        pg.keyboard.type("122.0600")
        pg.wait_for_timeout(300)
        x1 = use.bounding_box()["x"]
        bb = use.bounding_box()
        pg.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        pg.wait_for_timeout(500)
        data = pg.locator(f"{hs} textarea").first.input_value()
        if abs(x1 - x0) > 1 or "found 122.0600" not in data:
            bad("HRMS", f"Use moved {round(x1 - x0)} px as the ppm appeared; one click on it gave {data!r}")
    except Exception as e:  # noqa: BLE001
        bad("HRMS", str(e).splitlines()[0][:160])
    ctx.close()

    # big amounts and tiny equivalents are shown whole; Yield stays in view
    for width in (1280, 1920):
        ctx, pg = page(width)
        try:
            pg.goto(f"{B}/experiments/{big['id']}")
            pg.wait_for_selector("table.grid td.yield", timeout=15000)
            pg.wait_for_timeout(300)
            out = pg.evaluate("""() => [...document.querySelectorAll('.grid-wrap')].flatMap(w => {
                const box = w.getBoundingClientRect(), why = []
                for (const y of w.querySelectorAll('td.yield')) if (y.getBoundingClientRect().right > box.right + 1) why.push('Yield is off to the side')
                for (const i of w.querySelectorAll('input:not(.text)')) if (i.scrollWidth > i.clientWidth + 1) why.push(`${i.getAttribute('aria-label')} shows ${i.value} cut`)
                return why })""")
            if out:
                bad("big amounts", f"at {width} px: " + "; ".join(out[:3]))
        except Exception as e:  # noqa: BLE001
            bad("big amounts", str(e).splitlines()[0][:160])
        ctx.close()

    # the Reactions page's yield filter works without Enter; Record change can be pressed once an amount is typed
    ctx, pg = page()
    try:
        asked: list[str] = []
        pg.on("request", lambda r: "/api/reactions?" in r.url and asked.append(r.url))
        pg.goto(B + "/reactions")
        pg.wait_for_selector("input[aria-label='Minimum yield']")
        pg.wait_for_load_state("networkidle")
        pg.click("input[aria-label='Minimum yield']")
        pg.keyboard.type("90")
        pg.wait_for_timeout(1500)
        if not any("min_yield=90" in u for u in asked):
            bad("Reactions filter", "typing 90 in “Yield at least” did nothing until Enter or a click elsewhere")
        pg.goto(B + "/inventory")
        pg.click("button:has-text('Adjust') >> nth=0", timeout=15000)
        pg.click("dialog[open] input[aria-label='Amount']")
        pg.keyboard.type("5")
        pg.wait_for_timeout(300)
        if pg.is_disabled("dialog[open] button:has-text('Record change')"):
            bad("Record change", "Record change stays greyed out while the amount is typed (until the box is left)")
        pg.keyboard.press("Escape")  # puts back the box's empty value; a second closes the dialog
        pg.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001
        bad("filters", str(e).splitlines()[0][:160])
    ctx.close()

    # Admin → Users: an arrow key on a role menu asks first
    other = httpx.post(B + "/api/users", headers=A, json={"username": "rrolechange", "password": "a-long-smoke-password", "must_change": False,
                                                          "full_name": "Rory Role", "role": "scientist"}).json()
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{admin_token}')")
    pg = ctx.new_page()
    try:
        pg.goto(B + "/admin")
        sel = "select[aria-label='Role for rrolechange']"
        pg.wait_for_selector(sel, timeout=15000)
        pg.focus(sel)
        pg.keyboard.press("ArrowDown")
        pg.wait_for_timeout(1000)
        role = next(u["role"] for u in httpx.get(B + "/api/users", headers=A).json() if u["id"] == other["id"])
        if role != "scientist":
            bad("role", f"one arrow key on the role menu made Rory {role} without asking")
        pg.wait_for_selector("dialog[open] >> text=Make Rory Role", timeout=5000)
        pg.click("dialog[open] button:has-text('Cancel')")
        pg.wait_for_timeout(300)
        if pg.input_value(sel) != "scientist":
            bad("role", f"after Cancel the menu shows {pg.input_value(sel)!r}")
    except Exception as e:  # noqa: BLE001
        bad("role", str(e).splitlines()[0][:160])
    ctx.close()

    # N before the notebooks have loaded
    ctx, pg = page()
    try:
        slow = Slow(pg, "**/api/notebooks?include_archived=true", 2.5, "GET")
        pg.goto(B + "/")
        pg.wait_for_selector(".palette-trigger", timeout=15000)
        pg.keyboard.press("n")
        pg.wait_for_selector("dialog[open]")
        slow.pump(6, until=lambda: not slow.held and slow.seen > 0)
        pg.wait_for_timeout(500)
        chosen = pg.eval_on_selector("dialog[open] select.select >> nth=0", "s => s.value") if pg.query_selector("dialog[open] select.select") else ""
        if chosen != str(nb["id"]):
            bad("N early", f"N pressed before the notebooks loaded: the dialog's notebook is {chosen!r}, not the default ({nb['id']})")
        slow.release()
    except Exception as e:  # noqa: BLE001
        bad("N early", str(e).splitlines()[0][:160])
    ctx.close()

    # a notebook deleted in another tab
    gone = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Deleted in the other tab"}).json()
    ctx, pg = page()
    try:
        pg.goto(f"{B}/notebooks/{gone['id']}")
        pg.wait_for_selector("h1 >> text=Deleted in the other tab")
        httpx.delete(f"{B}/api/notebooks/{gone['id']}", headers=H)  # the other tab
        pg.click("button:has-text('Delete notebook')")
        pg.click("dialog.modal button:has-text('Delete notebook')")
        pg.wait_for_selector("main >> text=was deleted, or you can no longer open it", timeout=8000)
        if not pg.query_selector("main a[href='/']") or "not found" in pg.inner_text("main").lower():
            bad("deleted elsewhere", f"the page says {pg.inner_text('main')[:120]!r}, without a link to the dashboard")
        # a new experiment typed for a notebook deleted meanwhile: said in the dialog, which keeps the title
        gone = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Deleted while typing"}).json()
        pg.goto(f"{B}/notebooks/{gone['id']}")
        pg.click("main button:has-text('New experiment')")
        pg.fill("dialog[open] input.input >> nth=0", "Typed in this tab")
        httpx.delete(f"{B}/api/notebooks/{gone['id']}", headers=H)
        pg.click("dialog[open] button.primary")
        pg.wait_for_selector("dialog[open] .error", timeout=8000)
        said, title = pg.inner_text("dialog[open] .error"), pg.input_value("dialog[open] input.input >> nth=0")
        if "was deleted" not in said or title != "Typed in this tab":
            bad("deleted elsewhere", f"creating an experiment in a notebook deleted meanwhile: the dialog says {said!r}, the title is {title!r}")
    except Exception as e:  # noqa: BLE001
        bad("deleted elsewhere", f"{str(e).splitlines()[0][:120]}; the page says {pg.inner_text('body')[:100]!r}")
    ctx.close()

    # ⌘K finds a sample code it hasn't loaded (here: none of the records are loaded), by asking Benchtop
    httpx.put(B + "/api/auth/me/sample-prefix", headers=H, json={"prefix": "PP"})
    code = httpx.post(f"{B}/api/experiments/{big['id']}/samples", headers=H, json={}).json()["code"]
    ctx, pg = page()
    try:
        pg.route(lambda url: "/api/experiments?limit=500" in url, lambda route: route.fulfill(status=200, body="[]", content_type="application/json"))
        pg.goto(B + "/")
        pg.wait_for_selector(".palette-trigger")
        pg.click(".palette-trigger")
        for typed in (code.replace("-", "–"), code.replace("-", "")):
            pg.fill(".palette-input", typed)
            pg.wait_for_timeout(800)
            top = pg.inner_text(".palette-list li[aria-selected='true']") if pg.query_selector(".palette-list li[aria-selected='true']") else ""
            if big["eid"] not in top:
                bad("⌘K code", f"{typed!r} in ⌘K: the first result is {top!r}, not {big['eid']}")
    except Exception as e:  # noqa: BLE001
        bad("⌘K code", str(e).splitlines()[0][:160])
    ctx.close()
    return found


def cut_short_lists(browser, admin_token: str) -> list[tuple[str, str, str, str]]:
    """0.31: a list that comes in pages says so when it's cut short, with Show more. Before, the Materials page stopped
    at 500, Search at 100 of each kind and the Inbox at 200, without a word (and Search's and the Inbox's counts were
    those of the page shown). Each check here failed on 0.30.1."""
    print("Lists cut short: Materials past 500, Search past 100, the Inbox past 200, and Show more…", flush=True)
    A = {"Authorization": "Bearer " + admin_token}
    found: list[tuple[str, str, str, str]] = []
    where = {"path": ""}

    def bad(kind: str, what: str) -> None:
        found.append(("cut short", where["path"], kind, what[:220]))
    httpx.post(B + "/api/users", headers=A, json={"username": "clister", "password": "a-long-smoke-password", "must_change": False,
                                                 "full_name": "Casey Lister", "role": "scientist"})
    token = httpx.post(B + "/api/auth/login", json={"username": "clister", "password": "a-long-smoke-password"}).json()["token"]
    H = {"Authorization": "Bearer " + token}
    httpx.patch(B + "/api/auth/me", headers=H, json={"preferences": {"guide_dismissed": True}})
    with httpx.Client(base_url=B, headers=H, timeout=30) as c:
        for i in range(520):
            c.post("/api/materials", json={"name": f"Cut-short check {i:03d}", "kind": "other"})
    total = int(httpx.get(B + "/api/materials?limit=1", headers=H).headers["X-Total-Count"])
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    rows = lambda sel: pg.locator(sel).count()  # noqa: E731

    def notice() -> str:
        el = pg.query_selector(".cut-short [role=status]")
        return el.inner_text().strip() if el else ""

    def press_show_more() -> None:
        """By keyboard, as someone who doesn't use a mouse would: the button takes the focus, Enter presses it."""
        pg.focus(".cut-short button")
        pg.keyboard.press("Enter")
    try:
        where["path"] = "/materials"
        pg.goto(B + "/materials")
        pg.wait_for_selector(".mat-row")
        pg.wait_for_timeout(500)
        want = f"Showing the first 500 of {total:,} materials."
        if rows(".mat-row") != 500 or notice() != want:
            bad("materials", f"{rows('.mat-row')} shown, and the page says {notice()!r} (expected 500 and {want!r})")
        else:
            press_show_more()
            pg.wait_for_function("document.querySelectorAll('.mat-row').length > 500", timeout=15000)
            pg.wait_for_timeout(300)
            if rows(".mat-row") != min(total, 1000):
                bad("show more", f"Show more gave {rows('.mat-row')} materials, not {min(total, 1000)}")
            if total <= 1000 and notice():
                bad("show more", f"everything is shown, and the page still says {notice()!r}")
        pg.fill("input[placeholder^='Search by name']", "Cut-short check 51")
        pg.wait_for_timeout(1200)
        if rows(".mat-row") != 10 or notice():  # 510 to 519: searching works as before, and nothing is left out
            bad("materials search", f"searching gave {rows('.mat-row')} materials (10 expected), notice {notice()!r}")

        where["path"] = "/search?q=Cut-short check"
        pg.goto(B + "/search?q=" + "Cut-short%20check")
        pg.wait_for_selector(".search-results")
        pg.wait_for_timeout(800)
        tab = pg.inner_text("[role=tab]:has-text('Materials')").strip()
        if tab != "Materials 520":
            bad("search count", f"the Materials tab says {tab!r}, not 'Materials 520'")
        if notice() != "Showing the first 100 of 520 materials." or rows(".search-hit") != 100:
            bad("search", f"{rows('.search-hit')} results shown, and the page says {notice()!r}")
        else:
            press_show_more()
            pg.wait_for_function("document.querySelectorAll('.search-hit').length > 100", timeout=15000)
            if rows(".search-hit") != 200 or notice() != "Showing the first 200 of 520 materials.":
                bad("search show more", f"Show more gave {rows('.search-hit')} results; the page says {notice()!r}")
    except Exception as e:  # noqa: BLE001
        bad("pages", str(e).splitlines()[0][:160])
    ctx.close()

    # the Inbox, with 230 files waiting (made up here: the demo lab watches no folders)
    def item(i: int) -> dict:
        return {"id": 900000 + i, "path": f"plate {i:03d}.csv", "revision": 1, "state": "inbox", "note": "No experiment number in the name",
                "instrument": {"id": 1, "name": "Plate reader", "kind": "files"},
                "files": [{"n": 0, "name": f"plate {i:03d}.csv", "size": 120, "content_type": "text/csv"}], "size": 120,
                "arrived_at": "2026-11-01T10:00:00Z", "handled_at": None, "handled_by": None, "matched": [], "experiment": None,
                "follow_up": False, "for_you": False}

    def inbox(route) -> None:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(route.request.url).query)
        if qs.get("state", ["inbox"])[0] != "inbox":
            return route.fulfill(status=200, body="[]", content_type="application/json", headers={"X-Total-Count": "0"})
        offset, limit = int(qs.get("offset", ["0"])[0]), int(qs.get("limit", ["200"])[0])
        import json
        page = [item(i) for i in range(230)][offset:offset + min(limit, 500)]
        route.fulfill(status=200, body=json.dumps(page), content_type="application/json", headers={"X-Total-Count": "230"})
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
    pg = ctx.new_page()
    where["path"] = "/inbox"
    try:
        pg.route(lambda url: "/api/inbox?" in url, inbox)
        pg.goto(B + "/inbox")
        pg.wait_for_selector(".inbox-list")
        pg.wait_for_timeout(500)
        tab = pg.inner_text("[role=tab]:has-text('Waiting')").strip()
        if tab != "Waiting (230)":
            bad("inbox count", f"the tab says {tab!r}, not 'Waiting (230)'")
        if notice() != "Showing the first 200 of 230 files." or rows(".inbox-list > li") != 200:
            bad("inbox", f"{rows('.inbox-list > li')} shown, and the page says {notice()!r}")
        else:
            press_show_more()
            pg.wait_for_function("document.querySelectorAll('.inbox-list > li').length > 200", timeout=15000)
            pg.wait_for_timeout(300)
            if rows(".inbox-list > li") != 230 or notice():
                bad("inbox show more", f"Show more gave {rows('.inbox-list > li')} files; the page says {notice()!r}")
    except Exception as e:  # noqa: BLE001
        bad("inbox", str(e).splitlines()[0][:160])
    ctx.close()
    return found


def _qr_video(text: str, path: Path, w: int = 640, h: int = 480) -> None:
    """A camera's view of a QR code, as a Y4M file Chrome can use as its fake camera."""
    import segno
    m = segno.make(text, error="m").matrix  # rows of 0/1, without the quiet zone
    n = len(m)
    mod = min(w, h) // (n + 12)
    ox, oy = (w - n * mod) // 2, (h - n * mod) // 2
    y = bytearray([225]) * (w * h)
    for r in range(n):
        for c in range(n):
            if m[r][c]:
                for dy in range(mod):
                    start = (oy + r * mod + dy) * w + ox + c * mod
                    y[start:start + mod] = bytes([30]) * mod
    uv = bytes([128]) * ((w // 2) * (h // 2))
    with open(path, "wb") as f:
        f.write(f"YUV4MPEG2 W{w} H{h} F10:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(5):
            f.write(b"FRAME\n" + bytes(y) + uv + uv)


def scanning(browser, token: str) -> list[tuple[str, str, str, str]]:
    """0.32: scanning a bottle's barcode. On the Inventory page: a code typed (as a USB scanner types it) shows the bottle,
    whose status is then changed, and Scan next opens the scanner again; an unknown code is added as a new lot with the
    barcode filled in; a second bottle with the same barcode needs Add anyway; an experiment number opens it. On a
    phone over plain http (no live camera there) the photo of a barcode is read, offline, by the decoder Benchtop
    serves itself; the bench page's Check a bottle says which row it's for and uses it for that row, never over an
    edit made elsewhere, and warns about a bottle that isn't in the reaction. On a secure page the live camera reads
    a code and is turned off. Every check fails without the 0.32 scanner."""
    print("Scanning: Inventory, the add-lot form, a photo on the phone's bench page, and the live camera…", flush=True)
    H = {"Authorization": "Bearer " + token}
    found: list[tuple[str, str, str, str]] = []
    where = {"path": ""}

    def bad(kind: str, what: str) -> None:
        found.append(("scanning", where["path"], kind, what[:220]))

    def material(name: str, **kw) -> dict:
        r = httpx.post(B + "/api/materials", headers=H, json={"name": name, **kw})
        if r.status_code == 409:  # the demo lab has this structure already
            mid = int(re.search(r"MAT-(\d+)", r.json()["detail"]).group(1))
            return httpx.get(f"{B}/api/materials/{mid}", headers=H).json()
        return r.json()

    def lot(mat: dict, barcode: str, **kw) -> dict:
        return httpx.post(f"{B}/api/materials/{mat['id']}/lots", headers=H, json={"barcode": barcode, "amount": 25, "unit": "g", **kw}).json()

    def scan_api(code: str) -> list[dict]:
        return httpx.get(B + "/api/scan", headers=H, params={"code": code}).json()["lots"]

    fluoro = material("Scan check 2-bromo-5-fluoropyridine", smiles="Fc1ccc(Br)nc1")
    hexanes = material("Scan check hexanes", kind="solvent", density=0.66)
    a1 = lot(fluoro, "SMOKE-VIM-0001", purity=97, location="Shelf 2")
    a2 = lot(fluoro, "SMOKE-VIM-0002", purity=99.5, location="Shelf 3", supplier="Combi-Blocks")
    lot(hexanes, "SMOKE-VIM-0003", location="Solvent cabinet")
    lot(fluoro, "SMOKE-VIM-0004", expires_at="2020-01-01T00:00:00")
    nb = httpx.post(B + "/api/notebooks", headers=H, json={"name": "Scanning check"}).json()
    exp = httpx.post(B + "/api/experiments", headers=H, json={"title": "Pyridine coupling", "notebook_id": nb["id"]}).json()
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.12},
            {"role": "reagent", "name": fluoro["name"], "smiles": fluoro["smiles"], "material_id": fluoro["id"], "lot_id": a1["id"],
             "purity": 97, "equiv": 1.2, "basis": "equiv"}]
    httpx.post(f"{B}/api/experiments/{exp['id']}/entries", headers=H, json={"kind": "reaction", "title": "Coupling", "content": {"rows": rows}})
    entry = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["title"] == "Coupling")

    def page(**kw):
        ctx = browser.new_context(**kw)
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: bad("page error", str(e)))
        # a 409 is expected here twice: the second bottle with a barcode, and the reaction table changed elsewhere
        pg.on("console", lambda m: m.type == "error" and not any(i in m.text for i in IGNORE + ("409 (Conflict)",)) and bad("console error", m.text))
        return ctx, pg

    def type_code(pg, code: str) -> None:
        pg.wait_for_selector("#scan-typed")
        pg.fill("#scan-typed", code)
        pg.keyboard.press("Enter")

    # ---- Inventory, on a desktop: a barcode scanner types the code and presses Enter
    ctx, pg = page(viewport={"width": 1400, "height": 900})
    try:
        where["path"] = "/inventory"
        pg.goto(B + "/inventory")
        pg.click("button:text-is('Scan')")
        pg.wait_for_selector("dialog.modal #scan-typed")
        if pg.evaluate("document.activeElement && document.activeElement.id") != "scan-typed":
            bad("focus", "the code box doesn't have the keyboard focus, so a USB scanner's typing goes elsewhere")
        if pg.locator(".scan-view video").is_visible():
            bad("camera", "the camera started by itself on a desktop")
        type_code(pg, " smoke-vim-0002\t")
        pg.wait_for_selector(".lot-card", timeout=10000)
        card = pg.inner_text(".lot-card")
        if fluoro["name"] not in card or "SMOKE-VIM-0002" not in card or "Shelf 3" not in card or "25 g" not in card:
            bad("card", f"the scanned bottle's card says {card[:160]!r}")
        pg.select_option(".lot-card select", "empty")
        pg.wait_for_selector("text=Saved. Marked empty.", timeout=10000)
        if scan_api("SMOKE-VIM-0002")[0]["status"] != "empty":
            bad("status", "marking the scanned bottle empty didn't reach the server")
        if "This bottle is marked empty." not in pg.inner_text(".lot-card"):
            bad("status", "the card of an empty bottle doesn't say so")
        httpx.patch(f"{B}/api/lots/{a2['id']}", headers=H, json={"status": "available"})
        pg.click("button:text-is('Scan next')")
        pg.wait_for_selector("dialog.modal #scan-typed")
        # a code no lot has: add it as a new lot, barcode filled in
        type_code(pg, "SMOKE-NEW-0042")
        pg.wait_for_selector("text=No lot has the barcode", timeout=10000)
        pg.click("button:text-is('Add as a new lot…')")
        pg.fill("dialog.modal input[aria-label='Find a material']", "Scan check hexanes")
        pg.click("dialog.modal .list-row:has-text('Scan check hexanes')", timeout=10000)
        pg.wait_for_selector("#lot-barcode")
        if pg.input_value("#lot-barcode") != "SMOKE-NEW-0042":
            bad("add lot", f"the new lot's barcode box holds {pg.input_value('#lot-barcode')!r}, not the scanned code")
        pg.fill("dialog.modal input[aria-label='Amount']", "500")
        pg.keyboard.press("Tab")
        pg.click("dialog.modal button:text-is('Add lot')")
        pg.wait_for_timeout(1000)
        new = scan_api("SMOKE-NEW-0042")
        if len(new) != 1 or new[0]["material_id"] != hexanes["id"]:
            bad("add lot", f"after Add lot, the code finds {len(new)} lot(s)")
        # an experiment's number opens it
        pg.click("button:text-is('Scan')")
        type_code(pg, exp["eid"])
        pg.wait_for_url(f"**/experiments/{exp['id']}", timeout=10000)

        # ---- the add-lot form: a barcode another bottle has needs Add anyway
        where["path"] = f"/materials/{fluoro['id']}"
        pg.goto(f"{B}/materials/{fluoro['id']}")
        pg.click("button:text-is('Receive lot')")
        pg.fill("dialog.modal input[aria-label='Amount']", "5")
        pg.keyboard.press("Tab")
        pg.fill("#lot-barcode", "SMOKE-VIM-0001")
        pg.click("dialog.modal button:text-is('Add lot')")
        pg.wait_for_selector(".dup-warn", timeout=10000)
        warn = pg.inner_text(".dup-warn")
        if f"{fluoro['mid']}-L{a1['id']:04d}" not in warn or "already has this barcode" not in warn:
            bad("duplicate", f"the warning says {warn[:160]!r}")
        if len(scan_api("SMOKE-VIM-0001")) != 1:
            bad("duplicate", "a second bottle with the same barcode was added without asking")
        pg.click(".dup-warn button:text-is('Add anyway')")
        pg.wait_for_timeout(1000)
        if len(scan_api("SMOKE-VIM-0001")) != 2:
            bad("duplicate", "Add anyway didn't add the second bottle")
        where["path"] = "/inventory"
        pg.goto(B + "/inventory")
        pg.click("button:text-is('Scan')")
        type_code(pg, "SMOKE-VIM-0001")
        pg.wait_for_selector("text=2 bottles", timeout=10000)
        if pg.locator("dialog.modal .lot-pick").count() != 2:
            bad("duplicate", "scanning a shared barcode doesn't list both bottles")
    except Exception as e:  # noqa: BLE001
        bad("inventory", str(e).splitlines()[0][:160])
    ctx.close()

    # ---- the phone's bench page over plain http: a photo of the barcode, read by the decoder Benchtop serves
    where["path"] = f"/bench/{exp['id']}"
    shot = Path(tempfile.mkdtemp(prefix="benchtop-scan-")) / "barcode.png"
    import segno
    segno.make("SMOKE-VIM-0002", error="m").save(str(shot), kind="png", scale=10, border=4)
    ctx, pg = page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    ctx.add_init_script("Object.defineProperty(window, 'isSecureContext', { get: () => false })")  # as at http://192.168.1.20:8000
    fetched: list[str] = []
    pg.on("request", lambda r: fetched.append(r.url))
    try:
        pg.goto(f"{B}/bench/{exp['id']}")
        pg.click("button:text-is('Scan a bottle')")
        pg.wait_for_selector("dialog.modal button:text-is('Take a photo of the barcode')")
        if pg.locator(".scan-view").is_visible() or not pg.locator("text=Live scanning needs a secure (https) address").is_visible():
            bad("http", "over plain http the scanner doesn't offer a photo instead of a live view")
        pg.set_input_files("input[data-testid=scan-photo]", str(shot))
        pg.wait_for_selector(".check-bottle >> text=This bottle:", timeout=20000)
        text = pg.inner_text(".check-bottle")
        if f"{fluoro['name']}, reagent, 1.2 eq: weigh" not in text or "names a different bottle" not in text:
            bad("check", f"Check a bottle says {text[:200]!r}")
        zx = [u for u in fetched if "zxing" in u]
        if not any(u.endswith(".wasm") for u in zx) or any(not u.startswith(B) for u in zx):
            bad("decoder", f"the decoder's files came from {sorted(set(u.split('/assets/')[0] for u in zx)) or 'nowhere'}")
        # an edit made on the computer meanwhile isn't overwritten
        cur = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == entry["id"])
        meanwhile = httpx.put(f"{B}/api/entries/{entry['id']}", headers=H, json={
            "content": {**cur["content"], "conditions": {"temperature_c": 80}}, "expected_version": cur["version"]})
        if meanwhile.status_code != 200 or meanwhile.json()["version"] == cur["version"]:
            bad("setup", f"the edit made elsewhere wasn't saved: {meanwhile.status_code} {meanwhile.text[:120]}")
        pg.click("button:text-is('Use this bottle for it')")
        pg.wait_for_selector("text=changed on another screen", timeout=10000)
        after = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == entry["id"])
        if after["content"]["rows"][1]["lot_id"] != a1["id"] or after["content"]["conditions"].get("temperature_c") != 80:
            bad("conflict", "Use this bottle overwrote an edit made elsewhere")
        pg.click("button:text-is('Use this bottle for it')")
        pg.wait_for_selector("text=now uses this bottle", timeout=10000)
        r = next(e for e in httpx.get(f"{B}/api/experiments/{exp['id']}", headers=H).json()["entries"] if e["id"] == entry["id"])["content"]["rows"][1]
        if r["lot_id"] != a2["id"] or r["purity"] != 99.5:
            bad("use", f"after Use this bottle the row has lot {r['lot_id']} at {r['purity']} % (expected {a2['id']} at 99.5 %)")
        if "already uses this bottle" not in pg.inner_text(".check-bottle"):
            bad("use", "after Use this bottle the card doesn't say the table uses it")
        # a bottle that isn't in this reaction
        pg.click("button:text-is('Scan another bottle')")
        type_code(pg, "SMOKE-VIM-0003")
        pg.wait_for_selector("text=which isn't in this reaction", timeout=10000)
        if "This bottle is Scan check hexanes, which isn't in this reaction." not in pg.inner_text(".check-bottle"):
            bad("not in reaction", "the warning doesn't name the bottle's material")
        # an expired bottle of the right material: said plainly, and not offered for the row
        pg.click("button:text-is('Scan another bottle')")
        type_code(pg, "SMOKE-VIM-0004")
        pg.wait_for_selector(".check-bottle >> text=This bottle expired on", timeout=10000)
        if pg.locator("button:text-is('Use this bottle for it')").count():
            bad("expired", "an expired bottle is offered for the reaction row")
        over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if over > 2:
            bad("too wide", f"the bench page is {over}px wider than the phone")
    except Exception as e:  # noqa: BLE001
        bad("bench", str(e).splitlines()[0][:160])
    ctx.close()

    # ---- the live camera, on a secure page: Chrome's fake camera films a QR code; the scanner reads it and stops the camera
    where["path"] = "/inventory (live camera)"
    video = shot.parent / "camera.y4m"
    _qr_video("SMOKE-VIM-0003", video)
    cam = browser.browser_type.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                            f"--use-file-for-fake-video-capture={video}"])
    try:
        ctx = cam.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, permissions=["camera"])
        ctx.add_init_script(f"localStorage.setItem('benchtop.token', '{token}')")
        ctx.add_init_script("""(() => { const md = navigator.mediaDevices; if (!md) return; const get = md.getUserMedia.bind(md)
            window.__streams = []; md.getUserMedia = async (c) => { const s = await get(c); window.__streams.push(s); return s } })()""")
        on = "(window.__streams || []).flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length"
        # closed before it reads anything (the decoder held back): the camera goes off with the dialog
        pg = ctx.new_page()
        held: list = []
        pg.route("**/zxing_reader-*.wasm", lambda route: held.append(route))
        pg.goto(B + "/inventory")
        pg.click("button:text-is('Scan')")
        pg.wait_for_selector(".scan-view video", state="visible", timeout=10000)
        pg.wait_for_function("(window.__streams || []).length > 0", timeout=10000)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(500)
        if pg.locator("dialog.modal").count() or pg.evaluate(on):
            bad("camera left on", f"{pg.evaluate(on)} camera track(s) still on after the scanner was closed with Escape")
        pg.unroute("**/zxing_reader-*.wasm")
        for route in held:
            route.abort()
        pg.close()
        # the camera films the code: its bottle is shown, and the camera is off
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: bad("page error", str(e)))
        pg.goto(B + "/inventory")
        pg.click("button:text-is('Scan')")
        pg.wait_for_selector(".lot-card", timeout=20000)
        if "Scan check hexanes" not in pg.inner_text(".lot-card"):
            bad("live", "the live camera's code didn't show its bottle")
        if pg.evaluate(on) or not pg.evaluate("(window.__streams || []).length"):
            bad("camera left on", f"{pg.evaluate(on)} camera track(s) still on after the code was read")
        ctx.close()
    except Exception as e:  # noqa: BLE001
        bad("live camera", str(e).splitlines()[0][:160])
    finally:
        cam.close()
    return found


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
