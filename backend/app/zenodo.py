"""Zenodo's deposit API: every call Benchtop makes to Zenodo is in this file (https://developers.zenodo.org).

A deposit is made in four steps: create a draft deposition (with a reserved DOI), upload its files to the draft's
bucket, then publish it; a draft can be deleted until it's published. Each call carries the person's own personal
access token (scopes deposit:write and deposit:actions) as `Authorization: Bearer`. sandbox.zenodo.org is the same API
with separate accounts and tokens, and DOIs that don't resolve, for trying things out.

Errors come back as ZenodoError with a message for people, not an HTTP status. Tests swap in a fake Zenodo with
set_transport() (an httpx.MockTransport, see tests/fake_zenodo.py).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, BinaryIO, Optional
from urllib.parse import quote, urlsplit

import httpx

from . import __version__

ZENODO = "https://zenodo.org"
SANDBOX = "https://sandbox.zenodo.org"
_transport: Optional[httpx.BaseTransport] = None
TIMEOUT = httpx.Timeout(30.0, read=180.0, write=180.0)  # a big upload writes for a while, piece by piece
MAX_ERROR_BYTES = 200_000


class ZenodoError(Exception):
    """kind: token (not accepted) | scope (not allowed to) | unreachable | interrupted (cut off part-way) |
    too_large | refused (Zenodo said no, with a reason) | gone (no such deposition) | damaged | server | busy"""

    def __init__(self, message: str, kind: str = "server", status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status


def set_transport(t: Optional[httpx.BaseTransport]) -> None:
    global _transport
    _transport = t


def site_name(base: str) -> str:
    return urlsplit(base).netloc or base


def file_digests(src: bytes | Path) -> tuple[int, str, str]:
    """(size, md5, sha256): Zenodo checks uploads by MD5; .eln files and Benchtop record SHA-256."""
    md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    if isinstance(src, bytes):
        md5.update(src)
        sha.update(src)
        return len(src), md5.hexdigest(), sha.hexdigest()
    size = 0
    with open(src, "rb") as f:
        while chunk := f.read(1 << 20):
            size += len(chunk)
            md5.update(chunk)
            sha.update(chunk)
    return size, md5.hexdigest(), sha.hexdigest()


def _messages(r: httpx.Response) -> str:
    """Zenodo's reasons, from {"message": …, "errors": [{"field": …, "message(s)": …}]}."""
    try:
        body = r.json() if len(r.content) < MAX_ERROR_BYTES else {}
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    out = []
    for e in body.get("errors") or []:
        if not isinstance(e, dict):
            continue
        msgs = e.get("messages") or [e.get("message")]
        text = "; ".join(str(m) for m in msgs if m)
        field = str(e.get("field") or "").removeprefix("metadata.")
        if text:
            out.append(f"{field}: {text}" if field else text)
    if not out and body.get("message"):
        out.append(str(body["message"]))
    return "; ".join(out)[:600]


class Client:
    """One person's connection to one Zenodo site."""

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.site = site_name(self.base)
        self.token = token

    # ------------------------------------------------------------------ the calls

    def check(self) -> None:
        """Does Zenodo accept this token for making deposits? (Lists at most one of the person's depositions.)"""
        self._call("GET", "/api/deposit/depositions", params={"size": 1})

    def create(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """A new draft deposition. metadata["prereserve_doi"] = True reserves its DOI now."""
        return self._call("POST", "/api/deposit/depositions", json={"metadata": metadata}).json()

    def get(self, dep_id: int) -> dict[str, Any]:
        return self._call("GET", f"/api/deposit/depositions/{int(dep_id)}").json()

    def update(self, dep_id: int, metadata: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/api/deposit/depositions/{int(dep_id)}", json={"metadata": metadata}).json()

    def files(self, dep_id: int) -> list[dict[str, Any]]:
        data = self._call("GET", f"/api/deposit/depositions/{int(dep_id)}/files").json()
        return data if isinstance(data, list) else []

    def delete_file(self, dep_id: int, file_id: str) -> None:
        self._call("DELETE", f"/api/deposit/depositions/{int(dep_id)}/files/{quote(str(file_id), safe='')}", ok_missing=True)

    def upload(self, bucket_url: str, name: str, src: bytes | Path, md5: str) -> dict[str, Any]:
        """Put one file in the draft's bucket and check Zenodo got exactly what was sent (its MD5)."""
        self._same_site(bucket_url)
        url = f"{bucket_url.rstrip('/')}/{quote(name, safe='')}"
        body: bytes | BinaryIO = src if isinstance(src, bytes) else open(src, "rb")  # noqa: SIM115 - closed below
        try:
            r = self._call("PUT", url, content=body, headers={"Content-Type": "application/octet-stream"}, upload=name)
        finally:
            if not isinstance(body, bytes):
                body.close()
        try:
            got = r.json()
        except ValueError:
            got = {}
        checksum = str(got.get("checksum") or "")
        if checksum and checksum.removeprefix("md5:").lower() != md5:
            raise ZenodoError(f"{name} arrived at Zenodo damaged (its checksum doesn't match what Benchtop sent).", "damaged")
        return got

    def publish(self, dep_id: int) -> dict[str, Any]:
        return self._call("POST", f"/api/deposit/depositions/{int(dep_id)}/actions/publish").json()

    def discard(self, dep_id: int) -> None:
        """Delete a draft. One that's already gone is fine."""
        self._call("DELETE", f"/api/deposit/depositions/{int(dep_id)}", ok_missing=True)

    # ------------------------------------------------------------------ plumbing

    def _same_site(self, url: str) -> None:
        """The token only ever goes to the Zenodo site it belongs to, whatever address an answer points at."""
        a, b = urlsplit(url), urlsplit(self.base)
        if (a.scheme, a.netloc) != (b.scheme, b.netloc):
            raise ZenodoError(f"Zenodo pointed the upload at another address ({a.netloc}); nothing was sent there.", "refused")

    def _call(self, method: str, path: str, *, ok_missing: bool = False, upload: str = "", **kw: Any) -> httpx.Response:
        url = path if path.startswith("http") else self.base + path
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                   "User-Agent": f"Benchtop-ELN/{__version__} (+https://github.com/juleslafargue/benchtop-eln)",
                   **kw.pop("headers", {})}
        try:
            with httpx.Client(transport=_transport, timeout=TIMEOUT, follow_redirects=False) as c:
                r = c.request(method, url, headers=headers, **kw)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            if upload:
                raise ZenodoError(f"The connection to {self.site} was cut off while sending {upload}.", "interrupted") from e
            raise ZenodoError(f"Couldn't reach {self.site}. Check the internet connection and try again.", "unreachable") from e
        except httpx.HTTPError as e:
            raise ZenodoError(f"Couldn't talk to {self.site} ({e.__class__.__name__}).", "unreachable") from e
        if r.status_code < 300:
            return r
        if r.status_code == 404 and ok_missing:
            return r
        why = _messages(r)
        if r.status_code == 401:
            raise ZenodoError(f"{self.site} didn't accept your access token: it may have expired or been deleted, or be from "
                              f"the other Zenodo site (zenodo.org and sandbox.zenodo.org have separate accounts). Make a new "
                              f"one on {self.site} and enter it in Account settings.", "token", 401)
        if r.status_code == 403:
            raise ZenodoError(f"Your {self.site} token isn't allowed to do this. Make a new one with both the deposit:write "
                              f"and deposit:actions scopes ticked, and enter it in Account settings."
                              + (f" ({why})" if why else ""), "scope", 403)
        if r.status_code == 404:
            raise ZenodoError(f"{self.site} has no such draft any more (it may have been deleted on Zenodo's website).", "gone", 404)
        if r.status_code == 413:
            raise ZenodoError(f"{upload or 'A file'} is too large for {self.site}.", "too_large", 413)
        if r.status_code == 429:
            raise ZenodoError(f"{self.site} is busy and asked Benchtop to slow down; try again in a few minutes.", "busy", 429)
        if 400 <= r.status_code < 500:
            raise ZenodoError(f"{self.site} refused it" + (f": {why}" if why else f" (HTTP {r.status_code})") + ".", "refused",
                              r.status_code)
        if upload:
            raise ZenodoError(f"{self.site} had a problem while receiving {upload} (HTTP {r.status_code}).", "interrupted",
                              r.status_code)
        raise ZenodoError(f"{self.site} had a problem (HTTP {r.status_code}); try again in a while.", "server", r.status_code)
