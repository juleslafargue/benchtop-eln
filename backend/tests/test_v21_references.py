"""0.21: References sections (paste a DOI, get title, authors and journal) and two small launcher/check fixes.

- DOIs are found in whatever is pasted (doi:, doi.org links, address-bar %2F, a trailing full stop or bracket)
- lookups go to doi.org, which answers with the registrar's CSL-JSON (Crossref, DataCite…); markup in titles
  (<i>, <sub>, MathML) is taken out; redirects are only followed to registrars, never to a publisher's web page
- not found, unreachable, turned off, too large, not JSON: a plain message, and the section offers typing it in
- the section stores plain text; reports, Markdown, search and .eln exports show it; it round-trips between Benchtops;
  signing works as for any other section
- database step 5, so an older Benchtop refuses data that may hold a References section
- ./benchtop restore without a file lists the backups and the command to paste; relative paths work
- ./benchtop check says "1, and it matches its signature" instead of "all 1 match their signatures"
"""
from __future__ import annotations

import io
import os
import time
import json
import zipfile

import httpx
import pytest

from app import doi

PW = "lab-bench-pw-2026"

# What doi.org hands back for a Crossref DOI (application/vnd.citationstyles.csl+json), trimmed
GOTTLIEB = {
    "publisher": "American Chemical Society (ACS)", "issue": "21", "short-container-title": ["J. Org. Chem."],
    "published-print": {"date-parts": [[1997, 10]]}, "DOI": "10.1021/jo971176v", "type": "article-journal",
    "created": {"date-parts": [[2002, 7, 26]]}, "page": "7512-7515", "source": "Crossref",
    "title": "NMR Chemical Shifts of Common Laboratory Solvents as Trace Impurities", "prefix": "10.1021", "volume": "62",
    "author": [{"given": "Hugo E.", "family": "Gottlieb", "sequence": "first", "affiliation": []},
               {"given": "Vadim", "family": "Kotlyar", "sequence": "additional", "affiliation": []},
               {"given": "Abraham", "family": "Nudelman", "sequence": "additional", "affiliation": []}],
    "member": "316", "container-title": "The Journal of Organic Chemistry", "language": "en",
    "issued": {"date-parts": [[1997, 10, 1]]}, "URL": "http://dx.doi.org/10.1021/jo971176v",
    "ISSN": ["0022-3263", "1520-6904"], "container-title-short": "J. Org. Chem.", "reference": [{"key": f"ref{i}"} for i in range(40)],
}
# A title with markup, as Crossref keeps it for chemistry papers
MARKUP = {**GOTTLIEB, "DOI": "10.1002/anie.201000000", "title": ["Synthesis of C<sub>60</sub>&#8211;<i>tert</i>-Butyl Adducts "
          "via <mml:math><mml:mi>π</mml:mi></mml:math>-Stacking"], "container-title-short": "Angew. Chem. Int. Ed.", "page": None,
          "article-number": "e202312345", "issue": None, "volume": 63, "issued": {"date-parts": [[2024]]}}
# DataCite (Zenodo): no journal, a group as author, upper-case DOI
ZENODO = {"type": "dataset", "id": "https://doi.org/10.5281/zenodo.1234", "DOI": "10.5281/ZENODO.1234", "publisher": "Zenodo",
          "title": "Raw NMR data for the KX series", "author": [{"family": "Patel", "given": "Priya"}, {"literal": "Benchtop Demo Lab"}],
          "issued": {"date-parts": [["2025"]]}, "URL": "https://zenodo.org/record/1234"}

REGISTRY = {"10.1021/jo971176v": GOTTLIEB, "10.1002/anie.201000000": MARKUP, "10.5281/zenodo.1234": ZENODO}


def registrar(request: httpx.Request) -> httpx.Response:
    """doi.org redirects to the registrar's API, which answers in CSL-JSON."""
    host, path = request.url.host, request.url.path
    if host == "doi.org":
        d = httpx.URL(str(request.url)).path.lstrip("/")
        from urllib.parse import unquote
        d = unquote(d)
        if d.lower() == "10.1234/publisher-page":
            return httpx.Response(302, headers={"location": "https://publisher.example.com/article/1"})
        if d.lower() == "10.1234/http":
            return httpx.Response(302, headers={"location": "http://api.crossref.org/v1/works/x"})
        if d.lower() == "10.1234/no-csl":
            return httpx.Response(406)
        if d.lower() == "10.1234/loop":
            return httpx.Response(302, headers={"location": "https://doi.org/10.1234/loop"})
        if d.lower() == "10.1234/huge":
            return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/huge/transform"})
        if d.lower() == "10.1234/junk":
            return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/junk/transform"})
        if d.lower() == "10.1234/empty":
            return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/empty/transform"})
        key = next((k for k in REGISTRY if k == d.lower()), None)
        if key is None:
            return httpx.Response(404, text="DOI Not Found")
        api = "api.datacite.org/dois" if key.startswith("10.5281") else "api.crossref.org/v1/works"
        return httpx.Response(302, headers={"location": f"https://{api}/{key.replace('/', '%2F')}/transform"})
    assert request.headers["accept"] == doi.ACCEPT and "Benchtop-ELN" in request.headers["user-agent"]
    if host.endswith("crossref.org") or host.endswith("datacite.org"):
        if "/huge/" in path:
            return httpx.Response(200, content=b"[" + b'"x",' * 2_000_000 + b'"x"]')
        if "/junk/" in path:
            return httpx.Response(200, text="<html>Not JSON</html>")
        if "/empty/" in path:
            return httpx.Response(200, json={"type": "article-journal", "DOI": "10.1234/empty"})
        from urllib.parse import unquote
        raw = request.url.raw_path.decode().split("?")[0]
        key = unquote(raw.split("/")[-2] if raw.endswith("/transform") else raw.split("/")[-1]).lower()
        return httpx.Response(200, json=REGISTRY[key])
    raise AssertionError(f"Benchtop asked a host it shouldn't: {request.url}")


@pytest.fixture()
def registry():
    doi.set_transport(httpx.MockTransport(registrar))
    yield
    doi.set_transport(None)


# ---------------------------------------------------------------------------------------------------- DOIs


@pytest.mark.parametrize("pasted", [
    "10.1021/jo971176v", "  10.1021/jo971176v  ", "doi:10.1021/jo971176v", "DOI: 10.1021/jo971176v", "https://doi.org/10.1021/jo971176v",
    "http://dx.doi.org/10.1021/jo971176v", "doi.org/10.1021/jo971176v", "10.1021/jo971176v.", "<10.1021/jo971176v>",
    "https://doi.org/10.1021%2Fjo971176v", "10.1021/jo971176v)",
])
def test_the_doi_is_found_in_whatever_is_pasted(pasted):
    assert doi.normalise(pasted) == "10.1021/jo971176v"


@pytest.mark.parametrize("pasted", ["", "   ", "jo971176v", "10.1021", "11.1021/x", "10.12/x", "https://example.com/10.1021/x",
                                    "10.1021/" + "x" * 400, None, 42, ["10.1021/x"]])
def test_things_that_arent_dois_are_not(pasted):
    assert doi.normalise(pasted) == ""


def test_brackets_that_belong_to_the_doi_stay():
    assert doi.normalise("10.1016/S0040-4039(00)01234-5") == "10.1016/S0040-4039(00)01234-5"
    assert doi.normalise("(see 10.1016/S0040-4039(00)01234-5)".removeprefix("(see ")) == "10.1016/S0040-4039(00)01234-5"


def test_crossref_answer_becomes_a_reference(registry):
    r = doi.lookup("https://doi.org/10.1021/jo971176v")
    assert r == {"doi": "10.1021/jo971176v", "title": "NMR Chemical Shifts of Common Laboratory Solvents as Trace Impurities",
                 "authors": ["Gottlieb, Hugo E.", "Kotlyar, Vadim", "Nudelman, Abraham"], "journal": "J. Org. Chem.", "year": "1997",
                 "volume": "62", "issue": "21", "pages": "7512-7515", "note": ""}
    assert doi.plain(r) == ("Gottlieb, Hugo E.; Kotlyar, Vadim; Nudelman, Abraham. NMR Chemical Shifts of Common Laboratory Solvents "
                            "as Trace Impurities. J. Org. Chem. 1997, 62 (21), 7512–7515. DOI: 10.1021/jo971176v")


def test_the_doi_keeps_the_capitals_it_was_pasted_with(registry):
    assert doi.lookup("10.1021/JO971176V")["doi"] == "10.1021/JO971176V"  # Crossref answers 10.1021/jo971176v
    assert doi.lookup("10.5281/zenodo.1234")["doi"] == "10.5281/zenodo.1234"  # DataCite answers 10.5281/ZENODO.1234


def test_markup_in_titles_is_taken_out_and_article_numbers_stand_in_for_pages(registry):
    r = doi.lookup("10.1002/anie.201000000")
    assert r["title"] == "Synthesis of C60–tert-Butyl Adducts via π-Stacking"
    assert r["pages"] == "e202312345" and r["volume"] == "63" and r["year"] == "2024" and r["issue"] == ""
    assert r["journal"] == "Angew. Chem. Int. Ed."


def test_datacite_answer_uses_the_publisher_and_group_authors(registry):
    r = doi.lookup("10.5281/zenodo.1234")
    assert r["journal"] == "Zenodo" and r["authors"] == ["Patel, Priya", "Benchtop Demo Lab"] and r["year"] == "2025"


def test_a_hundred_and_fifty_authors_are_cut_to_a_hundred_with_et_al(registry):
    many = {**GOTTLIEB, "author": [{"family": f"Author{i}", "given": "A."} for i in range(150)]}
    r = doi.from_csl(many)
    assert len(r["authors"]) == 101 and r["authors"][-1] == "et al." and r["authors"][0] == "Author0, A."


@pytest.mark.parametrize("pasted,error,message", [
    ("10.9999/no-such-paper", doi.NotFound, "No DOI 10.9999/no-such-paper is registered"),
    ("10.1234/publisher-page", doi.DoiError, "doesn't share the paper's details"),
    ("10.1234/http", doi.DoiError, "doesn't share the paper's details"),
    ("10.1234/no-csl", doi.DoiError, "doesn't share the paper's details"),
    ("10.1234/loop", doi.DoiError, "kept redirecting"),
    ("10.1234/huge", doi.DoiError, "too large"),
    ("10.1234/junk", doi.DoiError, "wasn't a citation"),
    ("10.1234/empty", doi.DoiError, "no title or authors"),
])
def test_lookups_that_cant_work_say_why_in_plain_words(registry, pasted, error, message):
    with pytest.raises(error, match=message):
        doi.lookup(pasted)


def test_no_internet_says_so(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route to host")
    doi.set_transport(httpx.MockTransport(down))
    try:
        with pytest.raises(doi.DoiError, match="Couldn't reach doi.org. Check the internet connection"):
            doi.lookup("10.1021/jo971176v")
    finally:
        doi.set_transport(None)


def test_lookup_endpoint(client, users, registry):
    a = users["alice"]
    r = client.get("/api/doi", params={"q": "doi:10.1021/jo971176v"}, headers=a)
    assert r.status_code == 200 and r.json()["journal"] == "J. Org. Chem."
    assert client.get("/api/doi", params={"q": "not a doi"}, headers=a).status_code == 400
    r = client.get("/api/doi", params={"q": "10.9999/nothing"}, headers=a)
    assert r.status_code == 404 and "Check it for typos" in r.json()["detail"]
    assert client.get("/api/doi", params={"q": "10.1234/publisher-page"}, headers=a).status_code == 502
    assert client.get("/api/doi", params={"q": "10.1021/jo971176v"}, headers=users["vic"]).status_code == 403  # viewers can't edit
    assert client.get("/api/doi", params={"q": "10.1021/jo971176v"}).status_code == 401


def test_lookups_turned_off_say_so(client, users, admin):
    r = client.get("/api/doi", params={"q": "10.1021/jo971176v"}, headers=users["alice"])  # conftest: BENCHTOP_DOI=off
    assert r.status_code == 503 and "turned off" in r.json()["detail"] and "by hand" in r.json()["detail"]
    assert "BENCHTOP_DOI" not in r.json()["detail"]  # 0.29: a scientist isn't shown the setting's name
    r = client.get("/api/doi", params={"q": "10.1021/jo971176v"}, headers=admin)  # an admin, who can turn it on, is
    assert r.status_code == 503 and "BENCHTOP_DOI=off" in r.json()["detail"]


# ---------------------------------------------------------------------------------------------------- the section


def _exp(client, h, nb, title="NMR impurities"):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def _refs(client, h, exp, items, title="References"):
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h, json={"kind": "references", "title": title, "content": {"items": items}})
    assert r.status_code == 201, r.text
    return r.json()


FULMER = {"doi": "10.1021/om100106e", "title": "NMR Chemical Shifts of Trace Impurities", "authors": ["Fulmer, Gregory R.", "Goldberg, Karen I."],
          "journal": "Organometallics", "year": "2010", "volume": "29", "issue": "9", "pages": "2176-2179", "note": "Grease at 1.26 ppm"}


def test_new_section_starts_empty(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook)
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "references", "title": "References"})
    assert r.status_code == 201 and r.json()["content"] == {"items": []}


def test_whatever_is_sent_is_stored_as_text_and_typing_isnt_tidied_away(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook)
    e = _refs(client, a, exp, [
        {"doi": "doi:10.1021/om100106e", "title": "Trace impurities ", "authors": "Fulmer, G. R.; ; Goldberg, K. I.", "year": 2010,
         "volume": {"x": 1}, "pages": ["1"], "note": "line one\nline two ", "extra": "<script>alert(1)</script>"},
        {},  # someone pressed "Type one in" and hasn't typed yet
        "not a reference", 42,
    ])
    items = e["content"]["items"]
    assert len(items) == 2
    assert items[0] == {"doi": "doi:10.1021/om100106e", "title": "Trace impurities ", "authors": ["Fulmer, G. R.", "Goldberg, K. I."],
                        "journal": "", "year": "2010", "volume": "", "issue": "", "pages": "", "note": "line one\nline two "}
    assert items[1] == {"doi": "", "title": "", "authors": [], "journal": "", "year": "", "volume": "", "issue": "", "pages": "", "note": ""}
    assert doi.link(items[0]) == "https://doi.org/10.1021/om100106e"


def test_sizes_are_capped(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook)
    e = _refs(client, a, exp, [{"title": "t" * 5000, "authors": ["x"] * 500, "note": "n" * 9000}] * 600)
    items = e["content"]["items"]
    assert len(items) == doi.MAX_ITEMS and len(items[0]["title"]) == 1000 and len(items[0]["authors"]) == 101 and len(items[0]["note"]) == 2000


def test_report_markdown_and_search_show_references(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Grease in CDCl3")
    _refs(client, a, exp, [FULMER, {"title": "A <b>bold</b> claim & more", "authors": ["Smith, J."], "doi": "not-a-doi", "journal": "Chem*"}, {}])

    page = client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
    assert "<ol class='refs'>" in page
    assert ("<li>Fulmer, Gregory R.; Goldberg, Karen I.. " not in page)
    assert ("Fulmer, Gregory R.; Goldberg, Karen I. NMR Chemical Shifts of Trace Impurities. <i>Organometallics</i> <b>2010</b>, "
            "<i>29</i> (9), 2176–2179. <a href=\"https://doi.org/10.1021/om100106e\">https://doi.org/10.1021/om100106e</a>") in page
    assert "Grease at 1.26 ppm" in page
    assert "A &lt;b&gt;bold&lt;/b&gt; claim &amp; more." in page and "<b>bold</b>" not in page
    assert "not-a-doi" in page and 'href="not-a-doi"' not in page and "https://doi.org/not-a-doi" not in page
    assert page.count("<li>") >= 2 and page.count("refs'><li>") == 1

    md = client.get(f"/api/experiments/{exp['id']}/export", params={"format": "md"}, headers=a).text
    assert ("1. Fulmer, Gregory R.; Goldberg, Karen I. NMR Chemical Shifts of Trace Impurities. *Organometallics* **2010**, *29* (9), "
            "2176–2179. <https://doi.org/10.1021/om100106e>") in md
    assert "   Grease at 1.26 ppm" in md
    assert "2. Smith, J. A \\<b\\>bold\\</b\\> claim & more. *Chem\\**. not-a-doi" in md
    assert "3." not in md.split("## References")[1].split("##")[0]  # the blank one is left out

    for q in ("Fulmer", "Organometallics", "om100106e", "1.26 ppm"):
        hits = client.get("/api/search", params={"q": q}, headers=a).json()["experiments"]
        assert any(h["id"] == exp["id"] for h in hits), q


def test_signed_record_with_references_verifies_and_is_read_only(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Signed with references")
    e = _refs(client, a, exp, [FULMER])
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    v = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    assert all(s["valid"] if "valid" in s else s.get("matches", True) for s in v["signatures"])
    r = client.put(f"/api/entries/{e['id']}", headers=a, json={"content": {"items": []}, "expected_version": e["version"]})
    assert r.status_code == 409


def _graph(data: bytes) -> dict[str, dict]:
    z = zipfile.ZipFile(io.BytesIO(data))
    root = z.namelist()[0].split("/")[0]
    return {n["@id"]: n for n in json.loads(z.read(f"{root}/ro-crate-metadata.json"))["@graph"]}


def test_eln_export_cites_papers_and_another_benchtop_gets_the_section_back(client, users, notebook):
    a, b = users["alice"], users["bob"]
    exp = _exp(client, a, notebook, "Exported with references")
    _refs(client, a, exp, [FULMER, {"title": "Unpublished results", "authors": ["Patel, P."], "note": "Group meeting, May 2026"}, {}])
    data = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content
    g = _graph(data)
    paper = g["https://doi.org/10.1021/om100106e"]
    assert paper["@type"] == "ScholarlyArticle" and paper["name"] == FULMER["title"] and paper["isPartOf"] == "Organometallics"
    people = [g[r["@id"]] for r in paper["author"]]
    assert [(p["@type"], p["name"], p["familyName"], p["givenName"]) for p in people] == [
        ("Person", "Gregory R. Fulmer", "Fulmer", "Gregory R."), ("Person", "Karen I. Goldberg", "Goldberg", "Karen I.")]
    assert paper["datePublished"] == "2010" and paper["pagination"] == "2176–2179" and paper["identifier"] == "https://doi.org/10.1021/om100106e"
    ds = next(n for n in g.values() if n.get("genre") == "experiment" and n.get("name") == "Exported with references")
    # RO-Crate: a citation must be an absolute URI, so only papers with a DOI are cited; the others are mentioned
    assert [c["@id"] for c in ds["citation"]] == ["https://doi.org/10.1021/om100106e"]
    other = [g[m["@id"]] for m in ds["mentions"] if g[m["@id"]]["@type"] == "ScholarlyArticle"]
    assert [(o["name"], o["description"]) for o in other] == [("Unpublished results", "Group meeting, May 2026")]  # the blank one is left out

    nb2 = client.post("/api/notebooks", headers=b, json={"name": "Bob's references"}).json()
    p = client.post("/api/eln/preview", headers=b, files={"file": ("x.eln", data, "application/zip")}).json()
    res = client.post(f"/api/eln/import/{p['token']}", headers=b, json={"notebook_id": nb2["id"], "items": [i["key"] for i in p["items"]]}).json()
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    sec = next(e for e in new["entries"] if e["kind"] == "references")
    src = next(e for e in client.get(f"/api/experiments/{exp['id']}", headers=a).json()["entries"] if e["kind"] == "references")
    assert sec["title"] == "References" and sec["content"] == src["content"]


def test_the_database_step_for_references(client):
    from app import migrations
    from app.db import engine
    assert migrations.SCHEMA_VERSION >= 5 and migrations.current_version(engine) == migrations.SCHEMA_VERSION
    assert migrations.MIGRATIONS[4].description == "References sections (papers by DOI)"


def test_the_demo_lab_has_a_references_section():
    import inspect
    from app import demo_lab
    src = inspect.getsource(demo_lab)
    assert '"references", "References"' in src and "10.1021/om100106e" in src


# ---------------------------------------------------------------------------------------------------- small fixes


def test_check_wording_for_one_record_and_none():
    from app.check import _all
    assert _all(1, "match their signatures", "and it matches its signature") == "1, and it matches its signature"
    assert _all(24, "match their signatures", "and it matches its signature") == "all 24 match their signatures"
    assert _all(0, "match their signatures", "and it matches its signature") == "none yet"
    assert _all(1234, "verify", "and it verifies") == "all 1,234 verify"


def test_check_report_never_says_all_1(client, users, notebook, tmp_path):
    """The full check on a database holding exactly one signed record: no "all 1"."""
    import sqlite3
    from app.check import check_files
    from app.config import settings
    from app.db import engine
    from app.models import ExperimentStatus  # noqa: F401
    a = users["alice"]
    exp = _exp(client, a, notebook, "Only signed record")
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    one = tmp_path / "one.db"
    src, c = sqlite3.connect(settings.data_dir / "benchtop.db"), sqlite3.connect(one)
    src.backup(c)
    src.close()
    # keep exactly one signed record and no attachments; the check reads the file alone, so no write-ahead log
    c.execute("UPDATE experiment SET status='open' WHERE id != ? AND status IN ('signed', 'witnessed')", (exp["id"],))
    c.execute("DELETE FROM attachment")
    c.commit()
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    engine.dispose()
    r = check_files(one, tmp_path / "no-attachments")
    detail = {x["name"]: x["detail"] for x in r["checks"]}
    assert detail["Signed records"] == "1, and it matches its signature"
    assert detail["Attached files"] == "none yet"
    assert not any(d.startswith("all 1 ") for d in detail.values())


# ---------------------------------------------------------------------------------------------------- the launcher

from test_v19_updates import Lab, _attach_env, make_program, needs_tools, venv  # noqa: E402,F401  (venv is a fixture)


@pytest.fixture
def lab(tmp_path, venv):  # noqa: F811
    root = make_program(tmp_path / "benchtop", "21.0.0")
    _attach_env(root, venv)
    lab = Lab(root, tmp_path / "data")
    lab.make_data()
    return lab


@needs_tools
def test_restore_without_a_file_lists_backups_and_the_command_to_paste(lab):
    r = lab.run("restore", ok=False)
    assert r.returncode == 1 and "No backups in" in r.out and "nothing to restore" in r.out
    lab.run("backup")
    time.sleep(1.1)  # names go by the second
    lab.run("backup")
    newest = lab.backups()[-1]
    r = lab.run("restore", ok=False)
    assert r.returncode == 1 and "Usage:" not in r.out
    assert "Which backup?" in r.out and newest.name in r.out and lab.backups()[0].name in r.out
    assert f"./benchtop restore {newest}" in r.out and f"./benchtop check {newest}" in r.out
    assert "moved aside, not deleted" in r.out
    assert lab.my_work() == ["yield 87%"]  # nothing was restored


@needs_tools
def test_check_and_restore_take_a_path_relative_to_where_you_are(lab):
    lab.run("backup")
    rel = os.path.relpath(lab.backups()[-1], lab.root)  # e.g. ../data/backups/benchtop-backup-….zip
    assert not rel.startswith("/")
    r = lab.run("check", rel)
    assert "not found" not in r.out and "All good." in r.out, r.out
    r = lab.run("restore", rel)
    assert "Restored" in r.out and "previous data was kept" in r.out, r.out
    assert lab.my_work() == ["yield 87%"]
