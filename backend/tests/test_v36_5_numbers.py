"""0.36.5: wrong numbers with no warning (found by the 0.36.2 stress test of instrument data and spectra).

- A spectrum's saved analysis keeps the spectrometer frequency typed in for a ¹H file that doesn't give it (and says
  so when none was), and whether negative ¹³C lines are listed; an older analysis keeps having neither.
- Instrument folders: an arrival missing for one look is taken when it's back; an empty-looking folder or an unreadable
  subfolder is a failed look, said on the Admin page; a name that isn't UTF-8 is left out with a message, not the whole
  folder; EXP- with 19 or more digits doesn't match; an idle look doesn't read every row again; a renamed or touched
  dataset isn't attached twice; folders inside each other are refused; the size limit in one unit; a right-to-left
  override in a name is written out.
- A small withdrawal from a big lot is recorded as it was; NMR prediction refuses a structure far too big at once.
"""
from __future__ import annotations

import pytest

PW = "lab-bench-pw-2026"


def _exp(client, h, title="Numbers"):
    nb = client.post("/api/notebooks", headers=h, json={"name": f"NB {title}"}).json()
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- spectra: what an analysis saves

@pytest.mark.parametrize("sent, kept", [
    ({"frequency": 60}, {"frequency": 60.0}),
    ({"frequency": "59.7"}, {"frequency": 59.7}),
    ({"frequency": None}, {"frequency": None}),      # a new analysis of a file without a frequency, none typed
    ({"frequency": 5000}, {"frequency": None}),      # not a spectrometer
    ({"frequency": "abc"}, {"frequency": None}),
    ({}, {}),                                        # an older analysis: no key, so the viewer keeps its 400 MHz J
    ({"negatives": True}, {"negatives": True}),
    ({"negatives": "yes"}, {}),
])
def test_a_spectrum_analysis_keeps_the_frequency_typed_in(client, users, sent, kept):
    h = users["alice"]
    exp = _exp(client, h, "Frequency")
    analysis = {"integrals": [[3.8, 4.4]], "ref_idx": 0, "ref_value": 2, "threshold": 0.05, **sent}
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h,
                    json={"kind": "file", "title": "Spectra", "content": {"attachment_ids": [], "analyses": {"7": analysis}}})
    assert r.status_code == 201, r.text
    sec = next(e for e in client.get(f"/api/experiments/{exp['id']}", headers=h).json()["entries"] if e["title"] == "Spectra")
    got = sec["content"]["analyses"]["7"]
    assert {k: got[k] for k in ("frequency", "negatives") if k in got} == kept
    assert got["integrals"] == [[3.8, 4.4]]


# ---------------------------------------------------------------- instrument folders

import os  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlalchemy import event  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import instruments  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import InstrumentFile  # noqa: E402

T0 = datetime(2032, 3, 1, 9, 0, 0)


def scan(seconds: float):
    return instruments.scan_all(now=T0 + timedelta(seconds=seconds))


@pytest.fixture()
def add(client, admin):
    made = []

    def _add(folder: Path, **kw):
        r = client.post("/api/instruments", headers=admin, json={"name": kw.pop("name", f"Folder {folder.name}"),
                                                                  "kind": kw.pop("kind", "files"), "folder": str(folder), **kw})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()
    yield _add
    for i in made:
        client.delete(f"/api/instruments/{i}", headers=admin)


def _states(inst_id):
    with Session(engine) as s:
        return {r.path: r.state for r in s.exec(select(InstrumentFile).where(InstrumentFile.instrument_id == inst_id)
                                                 .order_by(InstrumentFile.revision))}


def _inst(client, admin, inst):
    return next(i for i in client.get("/api/instruments", headers=admin).json()["instruments"] if i["id"] == inst["id"])


def _files(client, h, exp):
    return [a["filename"] for a in client.get(f"/api/experiments/{exp['id']}", headers=h).json()["attachments"]]


def test_an_arrival_absent_for_one_look_is_taken_when_it_is_back_unchanged(client, admin, users, tmp_path, add):
    """0.36.4: it became "vanished" and, back unchanged, was never attached, never in the Inbox, and nobody was told."""
    h = users["alice"]
    exp = _exp(client, h, "Back again")
    folder = tmp_path / "share"
    folder.mkdir()
    (folder / "other.txt").write_text("keeps the folder from looking empty")
    inst = add(folder)
    scan(0)
    f = folder / f"{exp['eid']} nmr.jdx"
    f.write_text("##TITLE=x\n")
    scan(60)
    os.rename(f, tmp_path / f.name)   # moved out for a moment (or the share hiccuped)
    scan(120)
    assert _states(inst["id"])[f.name] == "vanished"
    os.rename(tmp_path / f.name, f)   # back, the same size and date
    scan(180)
    scan(400)
    assert _states(inst["id"])[f.name] == "attached"
    assert f.name in _files(client, h, exp)


def test_a_folder_that_lists_empty_is_a_look_that_failed_not_everything_gone(client, admin, users, tmp_path, add):
    """An unmounted drive leaves its empty mount point: the arrivals waiting there aren't counted as gone, and the
    Admin page says what it saw."""
    h = users["alice"]
    exp = _exp(client, h, "Empty mount")
    folder = tmp_path / "mnt"
    folder.mkdir()
    (folder / "last year.csv").write_text("already there")  # a drive with files on it
    inst = add(folder)
    scan(0)
    f = folder / f"{exp['eid']} run.csv"
    f.write_text("1,2\n")
    scan(60)
    away = tmp_path / "away"
    os.rename(folder, away)
    folder.mkdir()
    scan(120)
    assert _states(inst["id"])[f.name] == "settling"
    assert "The folder looked empty, with 1 arrival still waiting" in _inst(client, admin, inst)["last_error"]
    folder.rmdir()
    os.rename(away, folder)
    scan(200)
    scan(400)
    assert _states(inst["id"])[f.name] == "attached"
    assert _inst(client, admin, inst)["last_error"] == ""


def test_a_subfolder_that_cant_be_read_is_said_and_its_files_wait(client, admin, users, tmp_path, add, monkeypatch):
    """os.walk skipped an unreadable subfolder without a word, so the files in it "vanished" (0.36.4)."""
    h = users["alice"]
    exp = _exp(client, h, "Locked subfolder")
    folder = tmp_path / "lc"
    (folder / "run1").mkdir(parents=True)
    (folder / "elsewhere.txt").write_text("x")
    inst = add(folder)
    scan(0)
    f = folder / "run1" / f"{exp['eid']} hplc.csv"
    f.write_text("1,2\n")
    scan(60)
    real = os.walk

    def walk(top, topdown=True, onerror=None, followlinks=False):  # run1 can't be listed this time (permissions)
        for dirpath, dirnames, filenames in real(top, topdown=topdown, followlinks=followlinks):
            if "run1" in dirnames and Path(dirpath) == folder:
                dirnames.remove("run1")
                onerror(PermissionError(13, "Permission denied", str(folder / "run1")))
            yield dirpath, dirnames, filenames
    monkeypatch.setattr(instruments.os, "walk", walk)
    scan(120)
    assert _states(inst["id"])[f"run1/{f.name}"] == "settling"
    msg = _inst(client, admin, inst)["last_error"]
    assert "Couldn't read the subfolder “run1” (Permission denied)" in msg, msg
    monkeypatch.setattr(instruments.os, "walk", real)
    scan(200)
    scan(400)
    assert _states(inst["id"])[f"run1/{f.name}"] == "attached"


def test_a_name_that_isnt_utf8_is_left_out_not_the_whole_folder(client, admin, users, tmp_path, add):
    """0.36.4: "Couldn't read the folder: 'utf-8' codec can't encode character '\\udce9'…", every look, for everything."""
    h = users["alice"]
    exp = _exp(client, h, "Latin-1 name")
    folder = tmp_path / "old pc"
    folder.mkdir()
    inst = add(folder)
    scan(0)
    (folder / f"{exp['eid']} good.txt").write_text("good")
    bad = os.fsencode(str(folder)) + b"/" + f"{exp['eid']} caf".encode() + b"\xe9.txt"
    with open(bad, "wb") as fh:
        fh.write(b"latin-1 name")
    scan(60)
    scan(300)
    assert f"{exp['eid']} good.txt" in _files(client, h, exp)
    msg = _inst(client, admin, inst)["last_error"]
    assert f"“{exp['eid']} caf�.txt” has a name that isn't written the way names are on this computer" in msg, msg
    assert "codec" not in msg and "\\udce9" not in msg
    r = client.post("/api/instruments/check", headers=admin, json={"folder": str(folder), "kind": "files"})
    assert r.status_code == 200 and r.json()["items"] == 1, r.text


def test_an_experiment_number_too_long_to_be_one_doesnt_match(client, admin, users, tmp_path, add):
    """0.36.4: EXP- and 19 or more digits overflowed: the arrival was stuck "settling" for ever, the folder check said
    "Number too large" (422), and the old-data page was a 404."""
    folder = tmp_path / "dates"
    folder.mkdir()
    (folder / "EXP-00042202611171530459999 old.txt").write_text("old")
    assert client.post("/api/instruments/check", headers=admin, json={"folder": str(folder), "kind": "files"}).status_code == 200
    inst = add(folder)
    scan(0)
    r = client.get(f"/api/instruments/{inst['id']}/old", headers=admin)
    assert r.status_code == 200 and r.json()["total"] == 1, r.text
    new = "EXP-00042202611171530450001 new.txt"
    (folder / new).write_text("new")
    scan(60)
    scan(300)
    assert _states(inst["id"])[new] == "inbox"
    assert instruments.experiment_numbers("EXP-000000000000000042 x", "EXP-0000000000000000042 y") == [42]  # 18 digits match


def test_an_idle_look_at_a_big_folder_doesnt_read_every_row_again(client, admin, tmp_path, add):
    """0.36.4 committed every 500 files and each commit made the session forget all the rows it had loaded: an idle look
    at 20,000 files read them back one query each (39,505 queries, 9-10 s every minute)."""
    folder = tmp_path / "big"
    for i in range(1500):
        d = folder / f"u{i % 10}" / f"m{i % 7}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"old {i}.csv").write_text("1\n")
    add(folder)
    scan(0)
    n = [0]

    def count(*_):
        n[0] += 1
    event.listen(engine, "before_cursor_execute", count)
    try:
        scan(60)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert n[0] < 50, f"{n[0]} queries for an idle look at 1,500 files"


def _bruker(root: Path, sample: str, expno: int, title: str = "") -> Path:
    d = root / sample / str(expno)
    (d / "pdata" / "1").mkdir(parents=True)
    (d / "acqus").write_text("##$NUC1= <1H>\n##$SW_H= 8000\n##$BF1= 400.13\n##$TD= 16\n##$BYTORDA= 0\n##$DTYPA= 0\n")
    (d / "fid").write_bytes(bytes(range(64)))
    (d / "pdata" / "1" / "title").write_text(title)
    return d


def test_a_dataset_renamed_or_touched_isnt_attached_again(client, admin, users, tmp_path, add):
    """Plain files were recognised by their bytes; a dataset's zip depends on its folder's name and its files' dates,
    so a renamed sample folder or a re-touched dataset was attached again (0.36.4)."""
    h = users["alice"]
    exp = _exp(client, h, "Same data")
    folder = tmp_path / "nmr"
    folder.mkdir()
    inst = add(folder, kind="bruker_nmr")
    scan(0)
    d = _bruker(folder, exp["eid"], 1)
    scan(10)
    scan(200)
    assert len(_files(client, h, exp)) == 1
    os.rename(folder / exp["eid"], folder / f"{exp['eid']} renamed")   # the sample folder renamed
    scan(300)
    scan(500)
    t = (T0 - datetime(1970, 1, 1)).total_seconds()
    for p in (folder / f"{exp['eid']} renamed" / "1").rglob("*"):  # and touched
        os.utime(p, (t, t))
    scan(600)
    scan(800)
    assert len(_files(client, h, exp)) == 1, _files(client, h, exp)
    states = _states(inst["id"])
    assert states[f"{exp['eid']} renamed/1"] == "attached"
    with Session(engine) as s:
        notes = [r.note for r in s.exec(select(InstrumentFile).where(InstrumentFile.instrument_id == inst["id"]))]
    assert any("The same data is already in" in (n or "") for n in notes), notes
    # different data in the same place is attached
    (folder / f"{exp['eid']} renamed" / "1" / "fid").write_bytes(bytes(range(1, 65)))
    scan(900)
    scan(1100)
    assert len(_files(client, h, exp)) == 2
    assert d  # (the first dataset's folder)


def test_folders_inside_each_other_are_refused(client, admin, tmp_path, add):
    """An unmatched file waited twice in the Inbox, once for each folder (0.36.4 accepted them without a word)."""
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    add(outer, name="Outer")
    r = client.post("/api/instruments", headers=admin, json={"name": "Inner", "kind": "files", "folder": str(outer / "inner")})
    assert r.status_code == 409 and "is inside" in r.json()["detail"] and "would arrive twice" in r.json()["detail"], r.text
    other = tmp_path / "other"
    other.mkdir()
    inner = add(other / ".." / "outer" / ".." / "other", name="Other")  # the same folder written another way: fine, it's new
    r = client.patch(f"/api/instruments/{inner['id']}", headers=admin, json={"folder": str(tmp_path)})
    assert r.status_code == 409 and "holds" in r.json()["detail"], r.text


def test_the_size_limit_is_said_in_one_unit(client, admin, users, tmp_path, add, monkeypatch):
    """0.36.4: "It's 2,147 MB, more than the 2,048 MB limit": its size in MB, its limit in MiB."""
    monkeypatch.setattr(settings, "instrument_max_mb", 1)
    folder = tmp_path / "sizes"
    folder.mkdir()
    inst = add(folder)
    scan(0)
    (folder / "a.raw").write_bytes(b"x" * 1_500_000)
    (folder / "b.raw").write_bytes(b"x" * 1_000_500)
    (folder / "c.raw").write_bytes(b"x" * 999_000)
    scan(60)
    scan(300)
    with Session(engine) as s:
        rows = {r.path: r for r in s.exec(select(InstrumentFile).where(InstrumentFile.instrument_id == inst["id"]))}
    assert rows["a.raw"].note.startswith("It's 1.5 MB, more than the 1 MB limit"), rows["a.raw"].note
    assert rows["b.raw"].note.startswith("It's 1.0005 MB, more than the 1 MB limit"), rows["b.raw"].note
    assert rows["c.raw"].state == "inbox" and "limit" not in (rows["c.raw"].note or "")


def test_a_right_to_left_override_in_a_name_is_shown_not_obeyed(client, admin, users, tmp_path, add):
    """U+202E made "rtl ‮gpj.exe" show as "rtl exe.jpg" in the Inbox and the record (0.36.4)."""
    h = users["alice"]
    exp = _exp(client, h, "Odd characters")
    folder = tmp_path / "rtl"
    folder.mkdir()
    add(folder)
    scan(0)
    (folder / f"{exp['eid']} rtl ‮gpj.exe").write_text("x")
    scan(60)
    scan(300)
    assert f"{exp['eid']} rtl [U+202E]gpj.exe" in _files(client, h, exp)
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("photo ‮gpj.exe", b"x", "image/jpeg")})
    assert r.status_code in (200, 201) and r.json()["filename"] == "photo [U+202E]gpj.exe", r.text
    assert instruments.shown_name("caf\udce9.txt") == "caf�.txt"


# ---------------------------------------------------------------- inventory amounts

def test_small_withdrawals_from_a_big_lot_are_recorded_as_they_were(client, users):
    """0.36.4 rounded a lot's amount to 6 decimals of its own unit: 300 × 0.4 mg from a 1 kg drum each recorded 0 (the
    drum stayed at 1.000000 kg), and 0.6 mg was recorded as 1 mg. The amount must always match the usage list."""
    h = users["alice"]
    m = client.post("/api/materials", headers=h, json={"name": "Kilogram drum 0.36.5"}).json()
    lot = client.post(f"/api/materials/{m['id']}/lots", headers=h, json={"amount": 1, "unit": "kg"}).json()
    for _ in range(300):
        assert client.post(f"/api/lots/{lot['id']}/transactions", headers=h, json={"amount": 0.4, "unit": "mg"}).status_code == 201
    r = client.post(f"/api/lots/{lot['id']}/transactions", headers=h, json={"amount": 0.6, "unit": "mg"})
    assert r.status_code == 201
    tx = client.get(f"/api/lots/{lot['id']}/transactions", headers=h).json()
    outs = [t["delta"] for t in tx if t["delta"] < 0]
    assert sorted(set(outs)) == [-6e-7, -4e-7]
    amount = r.json()["lot"]["amount"]
    assert abs(amount - (1 - 300 * 4e-7 - 6e-7)) < 1e-12, amount
    assert abs(amount - sum(t["delta"] for t in tx)) < 1e-12  # the amount is what the usage list adds up to
    # undoing one puts back exactly that
    last = next(t for t in tx if t["delta"] == -6e-7)
    r = client.post(f"/api/lots/transactions/{last['id']}/reverse", headers=h)
    assert r.status_code == 201 and abs(r.json()["lot"]["amount"] - (1 - 300 * 4e-7)) < 1e-12
    # and a lot used up exactly is empty, not a speck of float left over
    small = client.post(f"/api/materials/{m['id']}/lots", headers=h, json={"amount": 0.3, "unit": "g"}).json()
    for v in (0.1, 0.1, 0.1):
        r = client.post(f"/api/lots/{small['id']}/transactions", headers=h, json={"amount": v, "unit": "g"})
    assert r.json()["lot"]["amount"] == 0 and r.json()["lot"]["status"] == "empty"


# ---------------------------------------------------------------- NMR prediction

def test_nmr_prediction_refuses_a_structure_far_too_big_at_once(client, users):
    """0.36.4 had no limit: the time grew with the square of the size (a 10,000-character SMILES took 7 s), and any
    signed-in person could tie up a worker with one."""
    import time
    h = users["alice"]
    for smiles in ("C" * 10_000, "C" * 250):
        t = time.monotonic()
        r = client.get("/api/nmr/predict", headers=h, params={"smiles": smiles, "nucleus": "1H"})
        assert r.status_code == 422, r.text
        assert "too large to predict" in r.json()["detail"] and "up to 200 atoms other than hydrogen" in r.json()["detail"]
        assert time.monotonic() - t < 2
