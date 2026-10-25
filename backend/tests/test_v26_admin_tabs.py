"""0.26: Admin is split into tabs, each with its own address. A tab with a problem gets a red dot, from /api/admin/problems:
the dashboard's backup, data-check and disk-space warnings (Backups), and instrument folders with an error (Instruments).
Links in the admins' emails open the Backups tab."""
from __future__ import annotations

from collections import namedtuple

from app import diskspace
from app.config import settings
from app.db import engine

Usage = namedtuple("Usage", "total used free")


def test_only_admins_see_the_problems(client, admin, users):
    assert client.get("/api/admin/problems", headers=users["alice"]).status_code == 403
    r = client.get("/api/admin/problems", headers=admin)
    assert r.status_code == 200 and set(r.json()) == {"backups", "instruments"}


def test_backup_and_disk_warnings_mark_the_backups_tab(client, admin, monkeypatch):
    monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: Usage(500 * 10**9, 497 * 10**9, 3 * 10**9))
    monkeypatch.setattr(settings, "disk_warn_gb", 5.0)
    (w,) = client.get("/api/admin/problems", headers=admin).json()["backups"]
    assert w.startswith("Only 3.0 GB free") and w == client.get("/api/dashboard", headers=admin).json()["system"]["disk_warnings"][0]
    monkeypatch.setattr(settings, "disk_warn_gb", 0.0)
    assert client.get("/api/admin/problems", headers=admin).json()["backups"] == []


def test_an_instrument_folder_with_an_error_marks_the_instruments_tab(client, admin, tmp_path):
    from sqlmodel import Session

    from app.models import Instrument
    folder = tmp_path / "nmr"
    folder.mkdir()
    r = client.post("/api/instruments", headers=admin, json={"name": "NMR 400 (tabs test)", "kind": "files", "folder": str(folder)})
    iid = r.json()["id"]
    try:
        with Session(engine) as s:
            inst = s.get(Instrument, iid)
            inst.last_error = "The folder isn't there."
            s.add(inst)
            s.commit()
        assert "NMR 400 (tabs test): The folder isn't there." in client.get("/api/admin/problems", headers=admin).json()["instruments"]
        with Session(engine) as s:
            inst = s.get(Instrument, iid)
            inst.enabled = False  # switched off: its old error isn't something to look at
            s.add(inst)
            s.commit()
        assert not any("tabs test" in p for p in client.get("/api/admin/problems", headers=admin).json()["instruments"])
    finally:
        client.delete(f"/api/instruments/{iid}", headers=admin)


def test_admins_emails_link_to_the_backups_tab(client, admin, monkeypatch):
    from app import mailer, sitesettings
    sent = []
    monkeypatch.setattr(mailer, "synchronous", True)
    monkeypatch.setattr(mailer, "transport", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: Usage(500 * 10**9, 497 * 10**9, 3 * 10**9))
    monkeypatch.setattr(settings, "disk_warn_gb", 5.0)
    sitesettings.update("email", {"host": "smtp.example.org", "from_address": "eln@example.org", "password": "s3cret-app-pw"})
    client.patch("/api/auth/me", headers=admin, json={"email": "admin@example.org"})
    (settings.data_dir / diskspace.STATE).unlink(missing_ok=True)
    try:
        diskspace.email_if_low()
        assert len(sent) == 1 and "/admin/backups" in sent[0].get_body(("plain",)).get_content()
    finally:
        (settings.data_dir / diskspace.STATE).unlink(missing_ok=True)
        client.patch("/api/auth/me", headers=admin, json={"email": ""})
        sitesettings.update("email", {"host": "", "from_address": ""})
