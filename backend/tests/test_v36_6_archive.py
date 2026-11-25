"""v0.36.6: the archive PDF (and a record's PDF) with awkward records. A long word, SMILES or title ran off the page and
the rest was lost; a table of 30 columns was squeezed to a letter a line; Chinese, Arabic, Korean or Hindi text showed
as □ without a word; files not placed in a Files section weren't listed at all, though the signature covers them."""
from __future__ import annotations

import io
import re
from datetime import date

import pypdf
import pytest
from sqlmodel import Session

PW = "lab-bench-pw-2026"
LONG = "A" * 1500 + "END-OF-LONG-WORD"
SMILES = "CC(C)(C)OC(=O)N1CCC(CC1)" + "C(=O)NC(CC2=CC=CC=C2)" * 12 + "C(=O)O"


def _text(raw: bytes) -> str:
    """The PDF's text with the line breaks, the hair spaces it may break at (U+200A) and the page footers taken out."""
    t = "".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(raw)).pages)
    t = re.sub("[\u200a\n ]", "", t)
    return re.sub(r"Benchtoprecords\d{4}·(?:[A-Z]{3}-\d{5}·)?page\d+of\d+", "", t)


def _off_the_page(raw: bytes) -> list[str]:
    """Runs of text that reach past the right edge of their page (estimated at 0.62 em a character, DejaVu's widest)."""
    out = []
    for page in pypdf.PdfReader(io.BytesIO(raw)).pages:
        right = float(page.mediabox.width)

        def visit(text, cm, tm, font, size):
            x = tm[4] * cm[0] + cm[4]
            scale = (tm[0] * cm[0]) or 1
            if text.strip() and x + len(text.rstrip()) * size * abs(scale) * 0.55 > right + 2:
                out.append(text.strip()[:40])
        page.extract_text(visitor_text=visit)
    return out


@pytest.fixture(scope="module")
def awkward(client, users):
    h = users["alice"]
    nb = client.post("/api/notebooks", headers=h, json={"name": "Awkward 0.36.6"}).json()

    def record(title, sections=(), files=()):
        e = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], "sections": "no_chemistry"}).json()
        for name, data in files:
            assert client.post(f"/api/experiments/{e['id']}/attachments", headers=h, files={"file": (name, data)}).status_code == 201
        for kind, content in sections:
            assert client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": kind, "content": content}).status_code == 201
        assert client.post(f"/api/experiments/{e['id']}/sign", headers=h, json={"password": PW}).status_code == 200
        return e
    return {
        "long": record("Long word " + "W" * 120, [("text", {"html": f"<p>{LONG}</p>"}), ("text", {"html": f"<p>{SMILES}</p>"})]),
        "wide": record("Wide table", [("table", {"columns": [{"name": f"Heading {i}"} for i in range(30)],
                                                  "rows": [[f"c{i}r{j}" for i in range(30)] for j in range(3)]})]),
        "script": record("Синтез 合成", [("text", {"html": "<p>中文产率 العربية 한국어 हिन्दी</p>"})]),
        "files": record("Many files", [("text", {"html": "<p>files</p>"})],
                        files=[(f"run-{i:03d}.csv", f"x,y\n{i},{i}\n".encode()) for i in range(40)]),
    }


def _archive(client, users, awkward):
    from app import archive
    from app.db import engine
    from app.models import User
    me = client.get("/api/auth/me", headers=users["alice"]).json()["id"]
    with Session(engine) as s:
        span = archive.Span.make(date.today().replace(month=1, day=1), date.today().replace(month=12, day=31))
        data, info = archive.build_pdf(s, s.get(User, me), span, page_size="a4")
    return _text(data), info


def test_long_words_smiles_and_titles_wrap_instead_of_being_cut_off(client, users, awkward):
    text, _ = _archive(client, users, awkward)
    assert "END-OF-LONG-WORD" in text and SMILES in text and "W" * 120 in text
    one = client.get(f"/api/experiments/{awkward['long']['id']}/export?format=pdf", headers=users["alice"])
    assert one.status_code == 200 and "END-OF-LONG-WORD" in _text(one.content)
    assert _off_the_page(one.content) == []  # the text was there before too, but drawn past the edge of the paper


def test_a_wide_table_comes_in_parts_of_eight_columns(client, users, awkward):
    text, _ = _archive(client, users, awkward)
    assert "Columns1–7of30" in text and "Columns8–13of30,withthefirstcolumnagain" in text and "Columns25–30of30,withthefirstcolumnagain" in text
    assert all(f"c{i}r2" in text for i in range(30))


def test_characters_the_font_cant_draw_are_said_so(client, users, awkward):
    text, _ = _archive(client, users, awkward)
    assert "can'tdraw(" in text.replace("’", "'") and "Chinese,JapaneseorKorean" in text and "Arabic" in text
    # a record with none of them gets no such note
    one = _text(client.get(f"/api/experiments/{awkward['wide']['id']}/export?format=pdf", headers=users["alice"]).content)
    assert "can'tdraw" not in one.replace("’", "'")


def test_every_attached_file_is_listed_with_its_fingerprint(client, users, awkward):
    text, _ = _archive(client, users, awkward)
    assert "Attachedfiles(40)" in text and all(f"run-{i:03d}.csv" in text for i in range(40))
    assert "Notshowninasectionabove" in text
    from app.db import engine
    from app.models import Attachment
    from sqlmodel import select
    with Session(engine) as s:
        shas = [a.sha256 for a in s.exec(select(Attachment).where(Attachment.experiment_id == awkward["files"]["id"]))]
    assert len(shas) == 40 and all(sha in text for sha in shas)
