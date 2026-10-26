"""Publishing a signed record on Zenodo (0.28): an open dataset with a DOI, for a thesis, a journal's supporting
information or a funder's data rules.

Two steps. *Prepare* makes a draft on Zenodo, with a reserved DOI, and uploads the record's PDF and its .eln export
(every section, attached file with its SHA-256, and signature); the draft can still be deleted. *Publish* makes it
public and registers the DOI, which can't be undone. Each person uses their own Zenodo account (a personal access
token in Account settings), and only a record's author can publish it.

What was sent and the DOI it got are kept in the Publication table, never in the record: a signature covers exactly
what it covered before, and the record verifies as it did. The HTTP calls are in zenodo.py.
"""
from __future__ import annotations

import html
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

from . import __version__, audit, doi as doilib, sitesettings, zenodo
from .config import settings
from .models import (Comment, Entry, EntryKind, Experiment, ExperimentStatus, Publication, Role, Signature, User,
                     ZenodoAccount, utcnow)
from .services import content_hash, live_entries

LICENCES = {"cc-by-4.0": "CC BY 4.0", "cc0-1.0": "CC0 1.0"}
SITES = {"zenodo": zenodo.ZENODO, "sandbox": zenodo.SANDBOX}
MAX_FILE_BYTES = 50 * 10**9  # Zenodo takes up to 50 GB per record
PUBLISHABLE = (ExperimentStatus.signed, ExperimentStatus.witnessed)
ACTIVE = ("working", "draft", "failed", "published")
_busy: set[int] = set()  # records being sent to Zenodo right now (one at a time each)
_busy_lock = threading.Lock()


class PublishError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ====================================================================== settings

def server() -> str:
    url = (sitesettings.get("zenodo")["url"] or zenodo.ZENODO).strip().rstrip("/")
    return url if url.startswith(("https://", "http://")) else zenodo.ZENODO


def is_sandbox(url: str) -> bool:
    return (urlsplit(url).hostname or "").startswith("sandbox.")


def unavailable() -> str:
    """Why nothing can be published from this Benchtop, or "" when it can."""
    if settings.demo:
        return "This is the demo lab: nothing is ever published from it."
    if not sitesettings.get("zenodo")["enabled"]:
        return "Publishing on Zenodo is turned off on this Benchtop (an administrator can turn it on in Admin → Integrations)."
    return ""


def site_out() -> dict[str, Any]:
    url = server()
    cfg = sitesettings.public("zenodo")
    return {"enabled": cfg["enabled"], "url": url, "site": zenodo.site_name(url), "sandbox": is_sandbox(url),
            "community": cfg["community"], "fixed": cfg["fixed"], "demo": settings.demo, "unavailable": unavailable()}


# ====================================================================== people's tokens

def _account(session: Session, user: User) -> ZenodoAccount | None:
    return session.exec(select(ZenodoAccount).where(ZenodoAccount.user_id == user.id)).first()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def account_out(session: Session, user: User) -> dict[str, Any]:
    acc = _account(session, user)
    usable = bool(acc and sitesettings.decrypt(acc.token))
    return {**site_out(), "connected": bool(acc and usable and acc.server == server()),
            "token_site": zenodo.site_name(acc.server) if acc else None, "hint": acc.hint if acc else "",
            "unreadable": bool(acc and not usable), "connected_at": _iso(acc.connected_at) if acc else None,
            "checked_at": _iso(acc.checked_at) if acc else None}


def connect(session: Session, user: User, token: str) -> dict[str, Any]:
    """Keep someone's token, once Zenodo has accepted it."""
    if settings.demo:
        raise PublishError(409, "This is the demo lab: it never connects to Zenodo.")
    token = (token or "").strip()
    if not 20 <= len(token) <= 300 or re.search(r"\s", token):
        raise PublishError(422, "That doesn't look like a Zenodo token. Copy the whole token from Zenodo (Applications → "
                                "Personal access tokens); it's a long run of letters and numbers.")
    url = server()
    checked = None
    try:
        zenodo.Client(url, token).check()
        checked = utcnow()
    except zenodo.ZenodoError as e:
        if e.kind not in ("unreachable", "server", "busy"):
            raise PublishError(422, str(e)) from None  # a wrong token is never kept
    acc = _account(session, user) or ZenodoAccount(user_id=user.id, server=url, token="")
    acc.server, acc.token, acc.hint = url, sitesettings.encrypt(token), token[-4:]
    acc.connected_at, acc.checked_at = utcnow(), checked
    session.add(acc)
    audit.record(session, user, "zenodo_connected", "user", user.id, site=zenodo.site_name(url), checked=checked is not None)
    session.commit()
    return {**account_out(session, user), "checked": checked is not None}


def disconnect(session: Session, user: User) -> dict[str, Any]:
    acc = _account(session, user)
    if acc:
        session.delete(acc)
        audit.record(session, user, "zenodo_disconnected", "user", user.id, site=zenodo.site_name(acc.server))
        session.commit()
    return account_out(session, user)


def _client(session: Session, user: User, url: str) -> zenodo.Client:
    acc = _account(session, user)
    site = zenodo.site_name(url)
    token = sitesettings.decrypt(acc.token) if acc else ""
    if not acc or not token:
        raise PublishError(409, f"Connect your {site} account first: Account settings → Zenodo, with a personal access token "
                                "from Zenodo." + (" (The one you entered before can't be read any more: this Benchtop's "
                                                  "secret key changed. Enter it again.)" if acc else ""))
    if acc.server != url:
        raise PublishError(409, f"Your Zenodo token is for {zenodo.site_name(acc.server)}, but this needs {site} (separate "
                                f"accounts). Enter a token from {site} in Account settings → Zenodo.")
    return zenodo.Client(url, token)


# ====================================================================== who may publish

def _latest_attestation(session: Session, exp: Experiment) -> Signature | None:
    return session.exec(select(Signature).where(Signature.experiment_id == exp.id, Signature.meaning.in_(("author", "witness")))
                        .order_by(Signature.id.desc())).first()


def why_not(user: User, exp: Experiment) -> str:
    """Why this person can't publish this record, or ""."""
    if exp.is_template:
        return "Templates aren't published."
    if user.role == Role.viewer:
        return "Read-only accounts can't publish records."
    if exp.owner_id != user.id:
        return "Only the record's author can publish it, from their own Zenodo account."
    if exp.status == ExperimentStatus.voided:
        return "A voided record can't be published."
    if exp.status not in PUBLISHABLE:
        return "Only signed records can be published: sign it first."
    return ""


def _require(user: User, exp: Experiment) -> None:
    if (msg := unavailable()):
        raise PublishError(409, msg)
    if (msg := why_not(user, exp)):
        raise PublishError(403 if "author" in msg or "Read-only" in msg else 409, msg)


# ====================================================================== what goes to Zenodo

def family_first(name: str) -> str:
    """Zenodo lists creators as "Family, Given": "Hanna Weiss" → "Weiss, Hanna". A name with a comma is kept."""
    name = " ".join((name or "").split())
    if "," in name or " " not in name:
        return name
    given, _, family = name.rpartition(" ")
    return f"{family}, {given}"


def _plain(h: str) -> str:
    h = re.sub(r"<(br|/p|/h\d|/li|/tr|/div)\b[^>]*>", "\n", h or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    return "\n".join(" ".join(line.split()) for line in t.splitlines() if line.strip())


def _shorten(t: str, n: int = 1200) -> str:
    if len(t) <= n:
        return t
    cut = t[:n]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[:end + 1] if end > n // 2 else cut.rstrip() + "…"


AIM = re.compile(r"\b(aim|aims|objective|purpose|goal|summary|abstract|introduction|background)\b", re.I)


def _opening_text(entries: list[Entry]) -> str:
    """The Aim (or Objective, Summary…) section's text, or else the first text section's."""
    texts = [e for e in entries if e.kind == EntryKind.text and _plain((e.content or {}).get("html", ""))]
    pick = next((e for e in texts if AIM.search(e.title or "")), texts[0] if texts else None)
    return _shorten(_plain((pick.content or {}).get("html", ""))) if pick else ""


def _day(dt: datetime) -> str:
    return f"{dt.day} {dt:%B %Y}"


def _signed_line(session: Session, exp: Experiment) -> str:
    sigs = list(session.exec(select(Signature).where(Signature.experiment_id == exp.id).order_by(Signature.id)))
    author = next((s for s in reversed(sigs) if s.meaning == "author"), None)
    witness = next((s for s in reversed(sigs) if s.meaning == "witness"), None) if exp.status == ExperimentStatus.witnessed else None
    who = lambda s: s.signer_name or s.signer_username or "the author"  # noqa: E731
    line = f"Electronic lab notebook record {exp.eid} from Benchtop ELN"
    if author:
        line += f", signed by {who(author)} on {_day(author.signed_at)}"
    if witness:
        line += f" and witnessed by {who(witness)} on {_day(witness.signed_at)}"
    return line + "."


FILES_LINE = ("The PDF is the printable record. The .eln file (RO-Crate, the ELN Consortium's format, which other lab notebooks "
              "import) holds every section, every attached file with its SHA-256, and the signatures, in a form programs can read.")


def references(entries: list[Entry]) -> list[str]:
    """DOIs from the record's References sections."""
    out: list[str] = []
    for e in entries:
        if e.kind != EntryKind.references:
            continue
        for it in (e.content or {}).get("items") or []:
            d = doilib.normalise(str((it or {}).get("doi") or "")) if isinstance(it, dict) else ""
            if d and d.lower() not in {x.lower() for x in out}:
                out.append(d)
    return out[:500]


def _last_affiliation(session: Session, user: User) -> str:
    for p in session.exec(select(Publication).where(Publication.user_id == user.id).order_by(Publication.id.desc()).limit(5)):
        creators = (p.details or {}).get("creators") or []
        if creators and creators[0].get("affiliation"):
            return str(creators[0]["affiliation"])
    return ""


def default_details(session: Session, user: User, exp: Experiment) -> dict[str, Any]:
    owner = session.get(User, exp.owner_id)
    entries = live_entries(session, exp.id)
    opening = (exp.description or "").strip() or _opening_text(entries)
    description = "\n\n".join(p for p in (opening, _signed_line(session, exp), FILES_LINE) if p)
    return {"title": exp.title, "description": description,
            "creators": [{"name": family_first(owner.full_name or owner.username), "affiliation": _last_affiliation(session, user),
                          "orcid": owner.orcid or ""}],
            "keywords": [t for t in (exp.tags or []) if t][:30], "licence": "cc-by-4.0", "access": "open", "embargo_date": "",
            "include_comments": False}


def _names(exp: Experiment) -> tuple[str, str]:
    from .eln import _safe
    stem = _safe(f"{exp.eid} {exp.title}", 90)
    return f"{stem}.pdf", f"{stem}.eln"


def files_preview(session: Session, exp: Experiment, include_comments: bool) -> list[dict[str, Any]]:
    """What becomes public: the two files, and what's inside the .eln."""
    from .models import Attachment
    atts = list(session.exec(select(Attachment).where(Attachment.experiment_id == exp.id, Attachment.withdrawn_at == None)  # noqa: E711
                             .order_by(Attachment.id)))
    n_comments = len(session.exec(select(Comment.id).where(Comment.experiment_id == exp.id, Comment.deleted == False)).all())  # noqa: E712
    pdf, eln = _names(exp)
    inside = ["report.html (the printable record)", "record.benchtop.json (every section, table and signature, for programs)"]
    inside += [f"files/{a.filename}" for a in atts]
    if any(e.kind == EntryKind.reaction for e in live_entries(session, exp.id)):
        inside.append("structures as MOL/RXN files")
    inside.append("trusted-timestamp proofs of the signatures, where there are any")
    if include_comments and n_comments:
        inside.append(f"the discussion ({n_comments} comment{'s' if n_comments != 1 else ''})")
    return [{"name": pdf, "what": "The printable record (A4 PDF): every section, the signatures and the content fingerprint"},
            {"name": eln, "what": "The .eln export: the full record in the shared format of lab notebooks", "contains": inside,
             "attached": [{"filename": a.filename, "size": a.size} for a in atts], "size": sum(a.size for a in atts),
             "comments": n_comments}]


def clean_details(raw: Any) -> dict[str, Any]:
    """What the author chose in the preview, checked."""
    from fastapi import HTTPException

    from .routers.auth import clean_orcid
    if not isinstance(raw, dict):
        raise PublishError(422, "Send the details as an object")
    title = " ".join(str(raw.get("title") or "").split())
    if not title:
        raise PublishError(422, "Give it a title")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise PublishError(422, "Zenodo needs a description: a few sentences on what the record is")
    creators = []
    for c in raw.get("creators") or []:
        if not isinstance(c, dict):
            continue
        name = " ".join(str(c.get("name") or "").split())[:200]
        if not name:
            continue
        try:
            orcid = clean_orcid(str(c.get("orcid") or ""))
        except HTTPException as e:
            raise PublishError(422, f"{name}: {e.detail}") from None
        creators.append({"name": name, "affiliation": " ".join(str(c.get("affiliation") or "").split())[:300], "orcid": orcid or ""})
    if not creators:
        raise PublishError(422, "List at least one author")
    if len(creators) > 100:
        raise PublishError(422, "Up to 100 authors")
    kw = raw.get("keywords") or []
    keywords = []
    for k in kw if isinstance(kw, list) else str(kw).split(","):
        k = " ".join(str(k).split())[:100]
        if k and k.lower() not in {x.lower() for x in keywords}:
            keywords.append(k)
    licence = str(raw.get("licence") or "cc-by-4.0")
    if licence not in LICENCES:
        raise PublishError(422, "The licence must be CC BY 4.0 or CC0 1.0")
    access = str(raw.get("access") or "open")
    if access not in ("open", "embargoed"):
        raise PublishError(422, "Access must be open, or embargoed until a date")
    embargo = ""
    if access == "embargoed":
        try:
            when = date.fromisoformat(str(raw.get("embargo_date") or ""))
        except ValueError:
            raise PublishError(422, "Choose the date the files become public") from None
        today = datetime.now(timezone.utc).date()
        if when <= today:
            raise PublishError(422, "The embargo has to end after today")
        if when > today + timedelta(days=3660):
            raise PublishError(422, "Embargoes can last up to 10 years")
        embargo = when.isoformat()
    return {"title": title[:300], "description": description[:20_000], "creators": creators, "keywords": keywords[:30],
            "licence": licence, "access": access, "embargo_date": embargo, "include_comments": bool(raw.get("include_comments"))}


def _html_paragraphs(text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def zenodo_metadata(session: Session, exp: Experiment, d: dict[str, Any]) -> dict[str, Any]:
    md: dict[str, Any] = {
        "upload_type": "dataset", "title": d["title"], "description": _html_paragraphs(d["description"]),
        "publication_date": datetime.now(timezone.utc).date().isoformat(),
        "creators": [{"name": c["name"], **({"affiliation": c["affiliation"]} if c["affiliation"] else {}),
                      **({"orcid": c["orcid"]} if c["orcid"] else {})} for c in d["creators"]],
        "access_right": d["access"], "license": d["licence"], "prereserve_doi": True,
        "notes": f"Exported from Benchtop ELN {__version__}. The record's content fingerprint (SHA-256), as signed: "
                 f"{content_hash(session, exp)}",
    }
    if d["access"] == "embargoed":
        md["embargo_date"] = d["embargo_date"]
    if d["keywords"]:
        md["keywords"] = d["keywords"]
    refs = references(live_entries(session, exp.id))
    if refs:
        md["related_identifiers"] = [{"identifier": r, "relation": "references", "scheme": "doi"} for r in refs]
    community = sitesettings.get("zenodo")["community"].strip()
    if community:
        md["communities"] = [{"identifier": community}]
    return md


# ====================================================================== the record's publication

def current(session: Session, exp: Experiment) -> Publication | None:
    return session.exec(select(Publication).where(Publication.experiment_id == exp.id, Publication.status.in_(ACTIVE))
                        .order_by(Publication.id.desc())).first()


def _status(p: Publication) -> str:
    """A draft still marked as being sent when nothing is sending it (Benchtop stopped part-way) counts as failed."""
    if p.status == "working" and p.experiment_id not in _busy:
        return "failed"
    return p.status


def out(session: Session, p: Publication | None) -> dict[str, Any] | None:
    if p is None:
        return None
    st = _status(p)
    sandbox = is_sandbox(p.server)
    owner = session.get(User, p.user_id)
    error = p.error or ("Benchtop stopped while sending this record to Zenodo. Prepare it again, or discard the draft."
                        if st == "failed" and p.status == "working" else "")
    return {"id": p.id, "status": st, "server": p.server, "site": zenodo.site_name(p.server), "sandbox": sandbox,
            "deposition_id": p.deposition_id, "draft_url": p.draft_url, "doi": p.doi,
            "doi_url": f"https://doi.org/{p.doi}" if p.doi and not sandbox else "", "url": p.url, "files": p.files or [],
            "details": p.details or {}, "error": error, "user": {"id": owner.id, "username": owner.username,
                                                                 "full_name": owner.full_name} if owner else None,
            "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at), "published_at": _iso(p.published_at)}


def summary(session: Session, exp: Experiment) -> dict[str, Any] | None:
    """For the record's page: its DOI once published, or the state of a draft."""
    if exp.is_template:
        return None
    p = current(session, exp)
    if p is None:
        return None
    o = out(session, p)
    return {k: o[k] for k in ("status", "site", "sandbox", "doi", "doi_url", "url", "draft_url", "published_at", "error")}


def status(session: Session, user: User, exp: Experiment) -> dict[str, Any]:
    acc = account_out(session, user)
    return {**site_out(), "why_not": why_not(user, exp), "connected": acc["connected"], "token_site": acc["token_site"],
            "publication": out(session, current(session, exp))}


def preview(session: Session, user: User, exp: Experiment, include_comments: bool | None = None) -> dict[str, Any]:
    p = current(session, exp)
    details = (p.details if p and p.status in ("draft", "failed", "working") and p.details else None) \
        or default_details(session, user, exp)
    if include_comments is not None:
        details = {**details, "include_comments": include_comments}
    entries = live_entries(session, exp.id)
    return {**status(session, user, exp), "details": details, "licences": LICENCES,
            "files": files_preview(session, exp, bool(details.get("include_comments"))), "references": references(entries)}


# ====================================================================== prepare, publish, discard

def _claim(exp: Experiment) -> None:
    with _busy_lock:
        if exp.id in _busy:
            raise PublishError(409, f"Benchtop is already sending {exp.eid} to Zenodo; wait for it to finish.")
        _busy.add(exp.id)


def _release(exp: Experiment) -> None:
    with _busy_lock:
        _busy.discard(exp.id)


def _link(url: Any, base: str, fallback: str) -> str:
    """A link from Zenodo's answer, only if it's on the same Zenodo site (never a javascript: or another site's address)."""
    u, b = urlsplit(str(url or "")), urlsplit(base)
    return str(url) if u.scheme in ("https", "http") and u.netloc == b.netloc else fallback


def _reserved_doi(dep: dict[str, Any]) -> str:
    pre = ((dep.get("metadata") or {}).get("prereserve_doi") or {})
    return str(pre.get("doi") or dep.get("doi") or "") if isinstance(pre, dict) else str(dep.get("doi") or "")


def _is_published(dep: dict[str, Any]) -> bool:
    return bool(dep.get("submitted")) or dep.get("state") == "done"


def _mark_published(session: Session, user: User, exp: Experiment, p: Publication, dep: dict[str, Any]) -> None:
    links = dep.get("links") or {}
    rec = dep.get("record_id") or dep.get("id") or p.deposition_id
    p.status, p.error = "published", ""
    p.doi = str(dep.get("doi") or "") or _reserved_doi(dep) or p.doi
    p.url = _link(links.get("record_html") or links.get("latest_html") or links.get("html"), p.server,
                  f"{p.server}/records/{rec}")
    if "/deposit/" in p.url or "/uploads/" in p.url:  # the draft page, not the record
        p.url = f"{p.server}/records/{rec}"
    p.published_at = p.updated_at = utcnow()
    session.add(p)
    audit.record(session, user, "zenodo_publish", "experiment", exp.id, eid=exp.eid, site=zenodo.site_name(p.server),
                 deposition_id=p.deposition_id, doi=p.doi, url=p.url, content_hash=p.content_hash)
    session.commit()


def prepare(session: Session, user: User, exp: Experiment, raw: Any) -> dict[str, Any]:
    """Step 1: a draft on Zenodo with a reserved DOI, and the record's PDF and .eln uploaded to it. Run again on a draft
    (or one that failed part-way) to try again or change the details: the same draft gets fresh files."""
    from . import eln
    from fastapi import HTTPException

    from .routers.experiments import pdf_bytes
    _require(user, exp)
    details = clean_details(raw)
    url = server()
    p = current(session, exp)
    if p and _status(p) == "published":
        raise PublishError(409, f"{exp.eid} is already published on Zenodo, as {p.doi}.")
    if p and p.server != url:
        raise PublishError(409, f"{exp.eid} has a draft on {zenodo.site_name(p.server)}, but this Benchtop now publishes on "
                                f"{zenodo.site_name(url)}. Discard that draft first.")
    client = _client(session, user, url)
    metadata = zenodo_metadata(session, exp, details)
    _claim(exp)
    tmp: Path | None = None
    try:
        if p is None:
            p = Publication(experiment_id=exp.id, server=url, user_id=user.id)
        p.status, p.error, p.details, p.updated_at = "working", "", details, utcnow()
        session.add(p)
        session.commit()
        sig = _latest_attestation(session, exp)
        signed_hash = content_hash(session, exp)
        pdf_name, eln_name = _names(exp)
        try:
            pdf = pdf_bytes(session, exp)
        except HTTPException as e:
            raise PublishError(e.status_code if e.status_code < 500 else 502, f"Couldn't make the PDF: {e.detail}") from None
        tmp = eln.scratch_dir() / f"zenodo-{exp.id}-{datetime.now():%H%M%S%f}.eln"
        eln.export(session, user, [exp], tmp, f"{exp.eid} - {exp.title}",
                   f"{exp.eid} from Benchtop ELN, as published on Zenodo", with_comments=details["include_comments"])
        files = []
        for name, what, src in ((pdf_name, "pdf", pdf), (eln_name, "eln", tmp)):
            size, md5, sha = zenodo.file_digests(src)
            if size > MAX_FILE_BYTES:
                raise PublishError(413, f"{name} is {size / 1e9:.1f} GB; Zenodo takes up to {MAX_FILE_BYTES // 10**9} GB per "
                                        "record. Nothing was published.")
            files.append({"name": name, "what": what, "size": size, "md5": md5, "sha256": sha, "src": src})

        try:
            dep = None
            if p.deposition_id:
                try:
                    dep = client.get(p.deposition_id)
                except zenodo.ZenodoError as e:
                    if e.kind != "gone":
                        raise
                    p.deposition_id, p.bucket_url, p.draft_url, p.doi = None, "", "", ""  # deleted on Zenodo: start again
                if dep is not None and _is_published(dep):
                    _mark_published(session, user, exp, p, dep)  # published on Zenodo's website meanwhile
                    return out(session, p)
            if dep is None:
                dep = client.create(metadata)
                p.deposition_id = int(dep["id"])
            else:
                dep = client.update(p.deposition_id, metadata)
                for f in client.files(p.deposition_id):
                    client.delete_file(p.deposition_id, str(f.get("id") or ""))
            links = dep.get("links") or {}
            p.bucket_url = str(links.get("bucket") or "")
            p.draft_url = _link(links.get("html") or links.get("latest_draft_html"), url, f"{url}/uploads/{p.deposition_id}")
            p.doi = _reserved_doi(dep) or p.doi
            session.add(p)
            session.commit()  # the draft exists now: whatever happens next, it can be found and discarded
            if not p.bucket_url:
                raise zenodo.ZenodoError("Zenodo didn't say where to upload the files.", "server")
            for f in files:
                client.upload(p.bucket_url, f["name"], f["src"], f["md5"])
        except zenodo.ZenodoError as e:
            msg = str(e)
            if p.deposition_id:
                msg += (" The draft on Zenodo is not published: prepare it again to retry, or discard it."
                        if e.kind in ("interrupted", "damaged", "too_large", "unreachable", "server", "busy") else
                        " Nothing was published.")
            else:
                msg += " Nothing was sent."
            p.status, p.error, p.updated_at = "failed", msg, utcnow()
            session.add(p)
            audit.record(session, user, "zenodo_prepare_failed", "experiment", exp.id, eid=exp.eid,
                         site=zenodo.site_name(url), deposition_id=p.deposition_id, error=msg[:500])
            session.commit()
            raise PublishError(503 if e.kind == "unreachable" else 413 if e.kind == "too_large" else 502, msg) from None

        p.files = [{k: v for k, v in f.items() if k != "src"} for f in files]
        p.content_hash, p.signature_id = signed_hash, sig.id if sig else None
        p.status, p.error, p.updated_at = "draft", "", utcnow()
        session.add(p)
        audit.record(session, user, "zenodo_prepare", "experiment", exp.id, eid=exp.eid, site=zenodo.site_name(url),
                     deposition_id=p.deposition_id, doi=p.doi, content_hash=signed_hash,
                     files=[{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in p.files],
                     licence=details["licence"], access=details["access"], comments=details["include_comments"])
        session.commit()
        return out(session, p)
    except PublishError as e:
        if p is not None and p.id is not None and p.status == "working":
            p.status, p.error, p.updated_at = "failed", e.message, utcnow()
            session.add(p)
            session.commit()
        raise
    except BaseException:
        if p is not None and p.id is not None and p.status == "working":
            session.rollback()
            p.status, p.error, p.updated_at = "failed", "Something went wrong while preparing it; try again.", utcnow()
            session.add(p)
            session.commit()
        raise
    finally:
        _release(exp)
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def publish(session: Session, user: User, exp: Experiment, confirm: bool) -> dict[str, Any]:
    """Step 2, which can't be undone: the draft becomes a public record on Zenodo and its DOI is registered."""
    _require(user, exp)
    if not confirm:
        raise PublishError(422, "Publishing can't be undone: confirm that the files and the DOI become permanent and public")
    p = current(session, exp)
    if p is None or _status(p) in ("failed", "working"):
        raise PublishError(409, f"Prepare {exp.eid} on Zenodo first" + (", then publish it." if p is None else
                                                                         ": the last attempt didn't finish."))
    if p.status == "published":
        raise PublishError(409, f"{exp.eid} is already published, as {p.doi}.")
    sig = _latest_attestation(session, exp)
    if content_hash(session, exp) != p.content_hash or (sig.id if sig else None) != p.signature_id:
        raise PublishError(409, f"{exp.eid} has changed since its files went to Zenodo (it was witnessed, or signed again). "
                                "Prepare it again, so what's published matches the record.")
    client = _client(session, user, p.server)
    _claim(exp)
    try:
        dep = client.get(p.deposition_id)
        if _is_published(dep):
            _mark_published(session, user, exp, p, dep)
            return out(session, p)
        theirs: dict[str, str] = {}  # name → MD5, as far as Zenodo's answer says
        for f in dep.get("files") or []:
            if isinstance(f, dict) and (name := str(f.get("filename") or f.get("key") or "")):
                theirs[name] = str(f.get("checksum") or "").removeprefix("md5:").lower()
        ours = {f["name"]: f["md5"] for f in p.files or []}
        if theirs and (set(theirs) != set(ours) or any(theirs[n] and theirs[n] != ours[n] for n in ours)):
            raise PublishError(409, "The draft on Zenodo has different files from the ones Benchtop sent (they were changed "
                                    "on Zenodo's website). Prepare it again, or discard it.")
        try:
            dep = client.publish(p.deposition_id)
        except zenodo.ZenodoError:
            try:  # the answer may have been lost after Zenodo published it
                dep = client.get(p.deposition_id)
            except zenodo.ZenodoError:
                dep = {}
            if not _is_published(dep):
                raise
        _mark_published(session, user, exp, p, dep)
        return out(session, p)
    except zenodo.ZenodoError as e:
        p.error, p.updated_at = f"Publishing didn't work: {e} The draft is still there, not published; try again.", utcnow()
        session.add(p)
        session.commit()
        raise PublishError(503 if e.kind == "unreachable" else 502, p.error) from None
    finally:
        _release(exp)


def discard(session: Session, user: User, exp: Experiment) -> dict[str, Any]:
    """Delete the draft on Zenodo (allowed until it's published). The reserved DOI is never registered."""
    if user.role == Role.viewer or exp.owner_id != user.id:  # even when it's no longer signed: the author can tidy up
        raise PublishError(403, "Only the record's author can discard its draft on Zenodo.")
    p = current(session, exp)
    if p is None:
        raise PublishError(404, f"{exp.eid} has no draft on Zenodo")
    if p.status == "published":
        raise PublishError(409, "A published record can't be discarded: it stays on Zenodo for good.")
    _claim(exp)
    try:
        if p.deposition_id:
            if settings.demo:
                raise PublishError(409, unavailable())
            client = _client(session, user, p.server)
            try:
                dep = client.get(p.deposition_id)
                if _is_published(dep):
                    _mark_published(session, user, exp, p, dep)
                    raise PublishError(409, f"It was published on Zenodo meanwhile, as {p.doi}, so it can't be discarded.")
                client.discard(p.deposition_id)
            except zenodo.ZenodoError as e:
                if e.kind != "gone":
                    raise PublishError(503 if e.kind == "unreachable" else 502, f"Couldn't discard the draft: {e}") from None
        p.status, p.updated_at = "discarded", utcnow()
        session.add(p)
        audit.record(session, user, "zenodo_discard", "experiment", exp.id, eid=exp.eid, site=zenodo.site_name(p.server),
                     deposition_id=p.deposition_id, doi=p.doi)
        session.commit()
        return {"publication": None}
    finally:
        _release(exp)
