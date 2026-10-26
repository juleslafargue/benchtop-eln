"""A fake Zenodo for tests: the parts of the deposit API Benchtop uses (https://developers.zenodo.org), with its checks.

It checks the token and its scopes (deposit:write to make and change drafts, deposit:actions to publish), takes uploads
into each draft's bucket and answers with their MD5, validates metadata the way Zenodo does for the fields Benchtop
sends, reserves a DOI when asked and registers it on publishing, and refuses to delete or change a published record.

    fz = FakeZenodo("https://sandbox.zenodo.org")
    zenodo.set_transport(fz.transport())          # in-process, for pytest

    python tests/fake_zenodo.py --port 8794       # a real local server, for the browser test and screenshots:
                                                  # start Benchtop with BENCHTOP_ZENODO_URL=http://127.0.0.1:8794

Tokens: GOOD (both scopes), WRITE_ONLY (can't publish). Anything else is refused as Zenodo refuses a wrong token.
Failures to try: mode = "down" (unreachable); cut_upload = n (the n-th upload is cut off part-way);
damage_upload = True (the MD5 it reports doesn't match); max_file (bytes); publish_answer_lost = True (publishes,
then the answer never arrives).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

import httpx

GOOD = "fakezenodotoken0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH"
WRITE_ONLY = "fakezenodowriteonly0123456789abcdefghijklmnopqrstuvwxyz"
UPLOAD_TYPES = {"publication", "poster", "presentation", "dataset", "image", "video", "software", "lesson", "physicalobject", "other"}
LICENCES = {"cc-by-4.0", "cc0-1.0", "cc-by-sa-4.0", "cc-by-nc-4.0", "mit", "apache-2.0"}
ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _bad(field: str, message: str) -> httpx.Response:
    return _json(400, {"status": 400, "message": "Validation error.", "errors": [{"field": field, "messages": [message]}]})


class FakeZenodo:
    def __init__(self, base: str = "https://zenodo.org", doi_prefix: str = "10.5072"):
        self.base = base.rstrip("/")
        self.doi_prefix = doi_prefix
        self.tokens = {GOOD: {"deposit:write", "deposit:actions"}, WRITE_ONLY: {"deposit:write"}}
        self.deps: dict[int, dict[str, Any]] = {}
        self.owner: dict[int, str] = {}
        self.buckets: dict[str, int] = {}
        self.content: dict[int, dict[str, bytes]] = {}
        self.next_id = 7001
        self.mode = "ok"
        self.cut_upload: int | None = None
        self.damage_upload = False
        self.publish_answer_lost = False
        self.max_file = 50 * 10**9
        self.uploads = 0
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def published(self) -> list[dict[str, Any]]:
        return [d for d in self.deps.values() if d["submitted"]]

    def _token(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.url.params.get("access_token")

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("Name or service not known", request=request)
        url = str(request.url)
        if not url.startswith(self.base):
            return _json(404, {"message": "Not found", "status": 404})
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        token = self._token(request)
        scopes = self.tokens.get(token or "")
        if scopes is None:
            return _json(401, {"message": "The server could not verify that you are authorized to access the URL requested.",
                               "status": 401})

        m = re.fullmatch(r"/api/files/([0-9a-f-]+)/(.+)", request.url.raw_path.decode().split("?")[0])
        if m:
            return self._upload(request, token, scopes, m.group(1), m.group(2))
        if path == "/api/deposit/depositions":
            if "deposit:write" not in scopes:
                return _json(403, {"message": "The token has no deposit:write scope.", "status": 403})
            if method == "GET":
                return _json(200, [self._out(d) for i, d in self.deps.items() if self.owner[i] == token][:10])
            if method == "POST":
                return self._create(request, token)
        m = re.fullmatch(r"/api/deposit/depositions/(\d+)(/.*)?", path)
        if not m:
            return _json(404, {"message": "Not found", "status": 404})
        dep_id, rest = int(m.group(1)), m.group(2) or ""
        dep = self.deps.get(dep_id)
        if dep is None or self.owner[dep_id] != token:
            return _json(404, {"message": "PID does not exist.", "status": 404})
        if "deposit:write" not in scopes:
            return _json(403, {"message": "The token has no deposit:write scope.", "status": 403})
        if rest == "" and method == "GET":
            return _json(200, self._out(dep))
        if rest == "" and method == "PUT":
            if dep["submitted"]:
                return _json(400, {"status": 400, "message": "Deposition is published: use the edit action first."})
            md = (json.loads(request.content or b"{}") or {}).get("metadata") or {}
            if (bad := self._validate(md)) is not None:
                return bad
            dep["metadata"] = self._with_doi(dep_id, md)
            return _json(200, self._out(dep))
        if rest == "" and method == "DELETE":
            if dep["submitted"]:
                return _json(403, {"message": "You do not have permission to delete a published record.", "status": 403})
            self.deps.pop(dep_id)
            self.content.pop(dep_id, None)
            return httpx.Response(204)
        if rest == "/files" and method == "GET":
            return _json(200, self._files(dep_id))
        m2 = re.fullmatch(r"/files/(.+)", rest)
        if m2 and method == "DELETE":
            if dep["submitted"]:
                return _json(403, {"message": "Files of a published record can't be removed.", "status": 403})
            name = next((k for k in self.content.get(dep_id, {}) if self._fid(dep_id, k) == m2.group(1)), None)
            if name is None:
                return _json(404, {"message": "File does not exist.", "status": 404})
            self.content[dep_id].pop(name)
            return httpx.Response(204)
        if rest == "/actions/publish" and method == "POST":
            return self._publish(dep_id, dep, scopes)
        return _json(405, {"message": "Method not allowed", "status": 405})

    # ------------------------------------------------------------------ endpoints

    def _create(self, request: httpx.Request, token: str) -> httpx.Response:
        try:
            body = json.loads(request.content or b"{}") or {}
        except ValueError:
            return _json(400, {"status": 400, "message": "Could not parse the request."})
        md = body.get("metadata") or {}
        if md and (bad := self._validate(md)) is not None:
            return bad
        dep_id = self.next_id
        self.next_id += 2
        bucket = str(uuid.uuid4())
        self.buckets[bucket] = dep_id
        self.owner[dep_id] = token
        self.content[dep_id] = {}
        self.deps[dep_id] = {"id": dep_id, "bucket": bucket, "submitted": False, "state": "unsubmitted", "doi": "",
                             "metadata": self._with_doi(dep_id, md)}
        return _json(201, self._out(self.deps[dep_id]))

    def _upload(self, request: httpx.Request, token: str, scopes: set[str], bucket: str, key: str) -> httpx.Response:
        dep_id = self.buckets.get(bucket)
        if dep_id is None or dep_id not in self.deps or self.owner[dep_id] != token:
            return _json(404, {"message": "Bucket does not exist.", "status": 404})
        if "deposit:write" not in scopes:
            return _json(403, {"message": "The token has no deposit:write scope.", "status": 403})
        if request.method != "PUT":
            return _json(405, {"message": "Method not allowed", "status": 405})
        if self.deps[dep_id]["submitted"]:
            return _json(403, {"message": "The bucket of a published record is locked.", "status": 403})
        self.uploads += 1
        if self.cut_upload is not None and self.uploads == self.cut_upload:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        length = int(request.headers.get("content-length") or 0)
        if length > self.max_file:
            return _json(413, {"message": "File is larger than the maximum allowed.", "status": 413})
        data = request.read()
        if len(data) > self.max_file:
            return _json(413, {"message": "File is larger than the maximum allowed.", "status": 413})
        name = unquote(key)
        self.content[dep_id][name] = data
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if self.damage_upload:
            md5 = "0" * 32
        return _json(201, {"key": name, "size": len(data), "checksum": f"md5:{md5}", "mimetype": "application/octet-stream",
                           "links": {"self": f"{self.base}/api/files/{bucket}/{key}"}})

    def _publish(self, dep_id: int, dep: dict[str, Any], scopes: set[str]) -> httpx.Response:
        if "deposit:actions" not in scopes:
            return _json(403, {"message": "The token has no deposit:actions scope.", "status": 403})
        if dep["submitted"]:
            return _json(400, {"status": 400, "message": "Deposition is already published."})
        if not self.content.get(dep_id):
            return _bad("files", "Missing uploaded files. To publish you need to upload at least one file.")
        md = {k: v for k, v in dep["metadata"].items() if k != "prereserve_doi"}
        for field in ("title", "creators", "description", "upload_type"):
            if not md.get(field):
                return _bad(f"metadata.{field}", "Missing data for required field.")
        dep["submitted"], dep["state"] = True, "done"
        dep["doi"] = dep["metadata"]["prereserve_doi"]["doi"]
        if self.publish_answer_lost:
            raise httpx.ReadTimeout("The read operation timed out")
        return _json(202, self._out(dep))

    # ------------------------------------------------------------------ the shapes Zenodo answers with

    def _with_doi(self, dep_id: int, md: dict[str, Any]) -> dict[str, Any]:
        md = dict(md)
        if md.get("prereserve_doi") is True or (dep_id in self.deps and self.deps[dep_id]["metadata"].get("prereserve_doi")):
            md["prereserve_doi"] = {"doi": f"{self.doi_prefix}/zenodo.{dep_id}", "recid": dep_id}
        return md

    def _fid(self, dep_id: int, name: str) -> str:
        return hashlib.sha1(f"{dep_id}/{name}".encode(), usedforsecurity=False).hexdigest()[:24]

    def _files(self, dep_id: int) -> list[dict[str, Any]]:
        return [{"id": self._fid(dep_id, n), "filename": n, "filesize": len(b),
                 "checksum": hashlib.md5(b, usedforsecurity=False).hexdigest(),
                 "links": {"self": f"{self.base}/api/deposit/depositions/{dep_id}/files/{self._fid(dep_id, n)}"}}
                for n, b in self.content.get(dep_id, {}).items()]

    def _out(self, dep: dict[str, Any]) -> dict[str, Any]:
        i, b = dep["id"], self.base
        links = {"self": f"{b}/api/deposit/depositions/{i}", "html": f"{b}/deposit/{i}", "bucket": f"{b}/api/files/{dep['bucket']}",
                 "publish": f"{b}/api/deposit/depositions/{i}/actions/publish", "latest_draft_html": f"{b}/deposit/{i}"}
        out = {"id": i, "record_id": i, "conceptrecid": str(i - 1), "state": dep["state"], "submitted": dep["submitted"],
               "metadata": dep["metadata"], "files": self._files(i), "links": links, "title": dep["metadata"].get("title", "")}
        if dep["submitted"]:
            out["doi"] = dep["doi"]
            out["doi_url"] = f"https://doi.org/{dep['doi']}"
            links.update({"record_html": f"{b}/records/{i}", "latest_html": f"{b}/records/{i}", "doi": out["doi_url"]})
        return out

    def _validate(self, md: dict[str, Any]) -> httpx.Response | None:
        if not isinstance(md, dict):
            return _bad("metadata", "Not a valid object.")
        if md.get("upload_type") not in UPLOAD_TYPES:
            return _bad("metadata.upload_type", "Invalid upload type.")
        if not str(md.get("title") or "").strip():
            return _bad("metadata.title", "Missing data for required field.")
        if not str(md.get("description") or "").strip():
            return _bad("metadata.description", "Missing data for required field.")
        creators = md.get("creators")
        if not isinstance(creators, list) or not creators:
            return _bad("metadata.creators", "Missing data for required field.")
        for c in creators:
            if not isinstance(c, dict) or not str(c.get("name") or "").strip():
                return _bad("metadata.creators", "Each creator needs a name.")
            if c.get("orcid") and not ORCID.match(str(c["orcid"])):
                return _bad("metadata.creators", f"Invalid ORCID identifier: {c['orcid']}")
        if md.get("access_right") not in ("open", "embargoed", "restricted", "closed"):
            return _bad("metadata.access_right", "Invalid access right.")
        if md["access_right"] in ("open", "embargoed") and md.get("license") not in LICENCES:
            return _bad("metadata.license", "Invalid license.")
        if md["access_right"] == "embargoed":
            try:
                if date.fromisoformat(str(md.get("embargo_date"))) <= date.today():
                    return _bad("metadata.embargo_date", "Embargo date must be in the future.")
            except ValueError:
                return _bad("metadata.embargo_date", "Not a valid date.")
        for r in md.get("related_identifiers") or []:
            if not r.get("identifier") or r.get("relation") not in ("references", "isSupplementTo", "isCitedBy", "cites"):
                return _bad("metadata.related_identifiers", "Invalid related identifier.")
        for c in md.get("communities") or []:
            if not re.fullmatch(r"[a-z0-9_-]+", str((c or {}).get("identifier") or "")):
                return _bad("metadata.communities", "Invalid community identifier.")
        try:
            date.fromisoformat(str(md.get("publication_date") or date.today().isoformat()))
        except ValueError:
            return _bad("metadata.publication_date", "Not a valid date.")
        return None

    # ------------------------------------------------------------------ as a real local server

    def serve(self, port: int) -> ThreadingHTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                req = httpx.Request(self.command, f"http://{self.headers.get('host')}{self.path}",
                                    headers=dict(self.headers.items()), content=body)
                try:
                    resp = fake.handle(req)
                except httpx.HTTPError:
                    self.close_connection = True
                    return
                data = resp.content
                self.send_response(resp.status_code)
                for k in ("content-type",):
                    if k in resp.headers:
                        self.send_header(k, resp.headers[k])
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _any

            def log_message(self, *a: Any) -> None:
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8794)
    args = ap.parse_args()
    fz = FakeZenodo(f"http://127.0.0.1:{args.port}")
    fz.serve(args.port)
    print(f"Fake Zenodo on http://127.0.0.1:{args.port}; token: {GOOD}", flush=True)
    threading.Event().wait()
