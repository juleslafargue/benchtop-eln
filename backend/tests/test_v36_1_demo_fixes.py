"""0.36.1: fixes from a practice run of the demo.

1. Every archive PDF said "Each signature was also certified by an independent RFC 3161 timestamp service" and gave the
   openssl commands to check it, in a lab with trusted timestamps switched off too (the demo lab is one). Benchtop
   never claims what isn't true: the archive (cover and last page, with or without the PDF/A package), the .eln's
   HOW-TO-CHECK.txt and the printed record now say what is there: no timestamps, some (how many), or all.
(The browser's fixes are checked in frontend/tests/demo-fixes.test.mjs and tests/smoke_browser.py.)"""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app import archive, pdfa, sitesettings
from app.db import engine
from app.models import User
from tests import pdfa_checks
from tests.test_v34_archive import PW, dated, record, sign, tsa, witness  # noqa: F401 - tsa is a fixture


class _Text(str):
    """Text from a PDF, where a line break may come out as a space or as nothing: `in` compares without spaces."""
    def __contains__(self, phrase: object) -> bool:
        return "".join(str(phrase).split()) in "".join(self.split())

    def __add__(self, other: str) -> "_Text":
        return _Text(str(self) + " " + str(other))


def _flat(text: str) -> _Text:
    return _Text(" ".join(text.replace("’", "'").split()))


def _archive(year: int, notebook) -> tuple[str, str, dict]:
    """The archive of `year` in this notebook, as alice: the text of its cover and of its last page, and what's in it."""
    with Session(engine) as s:
        alice = s.exec(select(User).where(User.username == "alice")).one()
        pdf, info = archive.build_pdf(s, alice, archive.Span.year(year), notebook["id"], page_size="a4")
    marks = pdfa_checks.outline(pdf)
    how = [p for t, p in marks if t == "How to check this archive"][0]
    contents = [p for t, p in marks if t == "Contents"][0]
    cover = _flat(pdfa_checks.text(pdf, pages=range(contents - 1)))
    last = _flat(pdfa_checks.text(pdf, pages=range(how - 1, pdfa_checks.page_count(pdf))))
    info["files"] = list(pdfa_checks.attachments(pdf)) if info["archival"] else []
    return cover, last, info


def _timestamps(on: bool) -> None:
    sitesettings.update("timestamps", {"enabled": on, "tsa_url": "http://tsa.example.org"})


def _signed(client, users, notebook, title: str, year: int, *, witnessed: bool = False):
    e = record(client, users["alice"], notebook, title, spectrum=False)
    sign(client, users["alice"], e)
    if witnessed:
        witness(client, users["bob"], e)
    dated(e["id"], f"{year}-03-01T10:00:00")
    return e


CLAIM_ALL = "Each signature was also certified by an independent RFC 3161 timestamp service"


# ====================================================================== the archive PDF

def test_an_archive_without_timestamps_says_so_and_claims_none(client, users, notebook):
    _signed(client, users, notebook, "No timestamps, one", 2011)
    _signed(client, users, notebook, "No timestamps, two", 2011, witnessed=True)
    cover, last, info = _archive(2011, notebook)
    assert info["archival"] and len(info["files"]) == 2, "only each record's data is attached"
    for text in (cover, last):
        assert "also certified" not in text and "RFC 3161" not in text and "openssl ts" not in text
        assert "proofs of its trusted timestamps" not in text and "timestamp.zip" not in text
        assert "weren't timestamped" in text
    assert "none of their signatures was certified by an independent timestamp service" in last
    assert "2 files are attached to this PDF" in last
    assert "content_sha256" not in last, "no timestamp to compare the fingerprint with"
    assert "Checking a record's fingerprint" in last and "python3 -c" in last


def test_an_archive_where_some_signatures_were_timestamped_says_how_many(client, users, notebook, tsa):
    _timestamps(False)
    _signed(client, users, notebook, "Before timestamps were on", 2012, witnessed=True)
    _timestamps(True)
    _signed(client, users, notebook, "After timestamps were on", 2012)
    cover, last, info = _archive(2012, notebook)
    assert sum(1 for f in info["files"] if f.endswith("-timestamp.zip")) == 1
    assert "1 of the 3 signatures in this archive was also certified by an independent RFC 3161 timestamp service" in last
    assert "the others weren't timestamped" in last
    assert CLAIM_ALL not in last
    assert "openssl ts -verify" in last and "timestamp.zip" in last and "Unzip a timestamped signature's timestamp file" in last
    assert "for each signature that has one, the proof of its trusted timestamp" in cover
    assert "3 files are attached to this PDF" in last  # two records and one proof


def test_an_archive_where_every_signature_was_timestamped_keeps_todays_text(client, users, notebook, tsa):
    _signed(client, users, notebook, "Timestamped, one", 2013, witnessed=True)
    _signed(client, users, notebook, "Timestamped, two", 2013)
    cover, last, info = _archive(2013, notebook)
    assert sum(1 for f in info["files"] if f.endswith("-timestamp.zip")) == 3
    assert CLAIM_ALL in last and "openssl ts -verify" in last and "Unzip the signature's timestamp file" in last
    assert "content_sha256 in each of its timestamps" in last
    assert "and the proofs of its trusted timestamps are attached to this file" in cover
    assert "weren't timestamped" not in cover + last


@pytest.fixture()
def no_pikepdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "pikepdf", None)
    assert not pdfa.available()
    yield


def test_without_the_pdfa_package_the_archive_is_as_truthful(client, users, notebook, tsa, no_pikepdf):
    _timestamps(False)
    _signed(client, users, notebook, "Plain PDF, no timestamp", 2014)
    with Session(engine) as s:
        alice = s.exec(select(User).where(User.username == "alice")).one()
        pdf, info = archive.build_pdf(s, alice, archive.Span.year(2014), notebook["id"], page_size="a4")
    text = _flat(pdfa_checks.text(pdf))
    assert not info["archival"]
    assert "also certified" not in text and "openssl ts" not in text and "Proof" not in text and "timestamp proof," not in text
    assert "weren't timestamped" in text and "record.benchtop.json" in text

    _timestamps(True)
    _signed(client, users, notebook, "Plain PDF, timestamped", 2014)
    with Session(engine) as s:
        alice = s.exec(select(User).where(User.username == "alice")).one()
        pdf, _ = archive.build_pdf(s, alice, archive.Span.year(2014), notebook["id"], page_size="a4")
    text = _flat(pdfa_checks.text(pdf))
    assert "1 of the 2 signatures in this archive was also certified" in text and "openssl ts -verify" in text
    assert "each timestamped signature's Proof download" in text and "{" not in text
    assert CLAIM_ALL not in text


def test_what_the_archive_says_in_each_case():
    """The words themselves: none, some (singular and plural), all; and the openssl steps only when there's a token."""
    assert archive.stamp_claim(4, 0).startswith("The records in this archive weren't timestamped")
    assert archive.stamp_claim(4, 1).startswith("1 of the 4 signatures in this archive was also certified")
    assert archive.stamp_claim(4, 3).startswith("3 of the 4 signatures in this archive were also certified")
    assert archive.stamp_claim(4, 4).startswith(CLAIM_ALL)
    assert "openssl" not in archive._timestamp_section(4, 0, "Unzip the signature's Proof")
    assert "openssl ts -verify" in archive._timestamp_section(4, 2, "Unzip the signature's Proof")
    assert "weren't timestamped" in _flat(archive._inside(4, 0)) and "weren't timestamped" in _flat(archive._not_inside(4, 0))
    assert "for each signature that has one" in archive._inside(4, 2)
    assert "the proofs of its trusted timestamps are attached" in archive._inside(4, 4)


# ====================================================================== the .eln's HOW-TO-CHECK.txt

def _how(client, h, exp) -> tuple[str, list[str]]:
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/experiments/{exp['id']}/export.eln", headers=h).content))
    root = z.namelist()[0].split("/")[0]
    return z.read(f"{root}/HOW-TO-CHECK.txt").decode(), z.namelist()


def test_the_eln_guide_speaks_of_timestamps_only_as_far_as_there_are_some(client, users, notebook, tsa):
    a = users["alice"]
    _timestamps(False)
    none = _signed(client, users, notebook, "ELN, no timestamp", 2015, witnessed=True)
    how, names = _how(client, a, none)
    assert not any("/signatures/" in n for n in names)
    flat = _flat(how)
    assert "also certified" not in flat and "RFC 3161" not in flat and "openssl ts" not in flat and "signatures/ " not in flat
    assert "weren't timestamped" in flat and "content_sha256" not in flat
    assert "python3 -c" in how  # the fingerprint can still be checked

    # some: the author's signature without, the witness's with
    some = record(client, a, notebook, "ELN, witnessed with timestamps on", spectrum=False)
    sign(client, a, some)
    _timestamps(True)
    witness(client, users["bob"], some)
    how, names = _how(client, a, some)
    assert sum(1 for n in names if "/signatures/" in n) == 1
    flat = _flat(how)
    assert "1 of the 2 signatures in this file was also certified" in flat and "openssl ts -verify" in flat
    assert "for each signature that has one" in flat and CLAIM_ALL not in flat

    every = _signed(client, users, notebook, "ELN, timestamped", 2015, witnessed=True)
    how, names = _how(client, a, every)
    assert sum(1 for n in names if "/signatures/" in n) == 2
    flat = _flat(how)
    assert CLAIM_ALL in flat and "openssl ts -verify" in flat and "certificates expire" in flat
    assert "A trusted-timestamp proof (.zip) for each signature." in flat


# ====================================================================== the printed record

def test_the_printed_record_has_a_timestamp_column_only_with_timestamps(client, users, notebook, tsa):
    a = users["alice"]
    _timestamps(False)
    plain = _signed(client, users, notebook, "Printed without timestamps", 2016)
    report = client.get(f"/api/experiments/{plain['id']}/export", headers=a).text
    assert "Trusted timestamp" not in report and "not obtained" not in report
    pdf = client.get(f"/api/experiments/{plain['id']}/export?format=pdf", headers=a).content
    assert "Trusted timestamp" not in pdfa_checks.text(pdf) and list(pdfa_checks.attachments(pdf)) == [f"{plain['eid']}.json"]
    _timestamps(True)
    stamped = _signed(client, users, notebook, "Printed with timestamps", 2016)
    report = client.get(f"/api/experiments/{stamped['id']}/export", headers=a).text
    assert "Trusted timestamp" in report
