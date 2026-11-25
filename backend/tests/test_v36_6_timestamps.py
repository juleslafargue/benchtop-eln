"""v0.36.6: trusted timestamps and the service's certificate dates.

A timestamp counts only if the service's certificate was valid at the time the timestamp gives: one signed with an
expired or not-yet-valid certificate was accepted and shown as certified. A timestamp made while the certificate
was valid stays valid after it expires (as RFC 3161 and `openssl ts -verify` treat it), so old ones don't turn bad."""
import base64
import hashlib
import re

import pytest
from sqlmodel import Session, select

from app import check, sitesettings, timestamps
from app.db import engine
from app.models import SignatureTimestamp
from tests.fake_tsa import FakeTSA

PW = "lab-bench-pw-2026"


@pytest.fixture()
def use(monkeypatch):
    def put(fake):
        monkeypatch.delenv("BENCHTOP_TIMESTAMPS", raising=False)
        monkeypatch.setattr(timestamps, "synchronous", True)
        monkeypatch.setattr(timestamps, "transport", fake)
        monkeypatch.setattr(timestamps, "_skip_until", 0.0)
        sitesettings.update("timestamps", {"enabled": True, "tsa_url": "http://tsa.example.org"})
        return fake
    yield put
    sitesettings.update("timestamps", {"enabled": False})


def _signed(client, users, notebook, title):
    e = client.post("/api/experiments", headers=users["alice"], json={"title": title, "notebook_id": notebook["id"]}).json()
    assert client.post(f"/api/experiments/{e['id']}/sign", headers=users["alice"], json={"password": PW}).status_code == 200
    return client.get(f"/api/experiments/{e['id']}", headers=users["alice"]).json()


@pytest.mark.parametrize("valid, says", [((-400, -30), "had expired"), ((10, 400), "wasn't valid yet")])
def test_a_timestamp_from_an_expired_or_not_yet_valid_certificate_is_never_stored(client, users, notebook, use, valid, says):
    use(FakeTSA(valid=valid))
    exp = _signed(client, users, notebook, f"Stamped by a certificate {says}")
    t = exp["signatures"][-1]["timestamp"]
    assert t["status"] == "pending" and says in t["error"], t  # tried again later, never shown as certified
    with Session(engine) as s:
        row = s.exec(select(SignatureTimestamp).where(SignatureTimestamp.signature_id == exp["signatures"][-1]["id"])).one()
        assert row.token in (None, "")


def _token(fake: FakeTSA, digest: bytes) -> bytes:
    from asn1crypto import tsp
    return tsp.TimeStampResp.load(fake("http://tsa.example.org", timestamps.build_request(digest, 12345)))["time_stamp_token"].dump()


def _stored_by_an_older_benchtop(client, users, notebook, use, fake: FakeTSA, title: str) -> int:
    """A signature whose timestamp was fetched (and stored as certified) by a Benchtop that didn't check dates."""
    down = use(FakeTSA())
    down.mode = "down"
    exp = _signed(client, users, notebook, title)
    sid = exp["signatures"][-1]["id"]
    with Session(engine) as s:
        row = s.exec(select(SignatureTimestamp).where(SignatureTimestamp.signature_id == sid)).one()
        row.token = base64.b64encode(_token(fake, bytes.fromhex(row.digest))).decode()
        row.status, row.error, row.tsa_name = "ok", "", "Benchtop Test TSA"
        s.add(row)
        s.commit()
    return sid


def _named(sid: int) -> bool:
    """Does ./benchtop check name this signature's timestamp as one that doesn't verify? (Other tests' may be named too.)"""
    item = next(c for c in check.check_live()["checks"] if c["name"] == "Trusted timestamps")
    return bool(re.search(rf"\(signatures [^)]*\b{sid}\b", item["detail"])) and not item["ok"]


def _forget(sid: int) -> None:
    with Session(engine) as s:  # so later tests' data checks aren't about this one
        row = s.exec(select(SignatureTimestamp).where(SignatureTimestamp.signature_id == sid)).one()
        row.status, row.token = "failed", None
        s.add(row)
        s.commit()


def test_a_timestamp_made_while_the_certificate_was_valid_stays_valid_after_it_expires(client, users, notebook, use):
    """The service's certificate ran from 400 to 30 days ago; the timestamp says it was made 100 days ago."""
    old = FakeTSA(valid=(-400, -30), stamped_days_ago=100)
    digest = hashlib.sha256(b"a signature description").digest()
    assert timestamps.verify(_token(old, digest), digest)["ok"]
    sid = _stored_by_an_older_benchtop(client, users, notebook, use, old, "Certified, then the certificate expired")
    try:
        assert not _named(sid)
    finally:
        _forget(sid)


def test_the_data_check_names_a_stored_timestamp_whose_certificate_had_expired(client, users, notebook, use):
    expired = FakeTSA(valid=(-400, -30))
    digest = hashlib.sha256(b"another description").digest()
    info = timestamps.verify(_token(expired, digest), digest)
    assert not info["ok"] and "had expired" in info["error"], info
    sid = _stored_by_an_older_benchtop(client, users, notebook, use, expired, "Certified with an expired certificate")
    try:
        assert _named(sid)
    finally:
        _forget(sid)
