"""Trusted timestamps (RFC 3161) for signatures.

When someone signs, Benchtop sends the SHA-256 fingerprint of a short description of the signature (record,
meaning, signer, time, and the record's content hash) to an independent timestamp authority (TSA). The TSA
returns a token, signed with its own certificate, saying "this fingerprint existed at <time>". Only the
fingerprint leaves the computer, never the record. Anyone can check a token later with openssl, without
trusting Benchtop: the download for each signature includes the token, the exact text that was fingerprinted
and the commands.

Offline (or when the TSA is down) signing still works; the timestamp is fetched later and the delay is shown.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from sqlmodel import Session, select

from . import __version__, sitesettings
from .models import Experiment, Signature, SignatureTimestamp, utcnow

log = logging.getLogger("benchtop.timestamps")
TSAS = [  # free public RFC 3161 services
    {"name": "DigiCert", "url": "http://timestamp.digicert.com"},
    {"name": "Sectigo", "url": "http://timestamp.sectigo.com"},
    {"name": "GlobalSign", "url": "http://timestamp.globalsign.com/tsa/r6advanced1"},
    {"name": "FreeTSA", "url": "https://freetsa.org/tsr"},
]
synchronous = False
transport: Callable[[str, bytes], bytes] | None = None  # tests: a fake TSA
MAX_REPLY = 1_000_000  # bytes; real tokens are 2–10 KB
TOTAL_SECONDS = 60
MAX_ATTEMPTS = 48  # hourly retries for two days, then it's marked failed (an admin can retry)
_q: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()
_queued: set[int] = set()  # already waiting in _q (hourly retries and "Retry now" don't add duplicates)
_skip_until = 0.0          # after the service couldn't be reached, don't spend a timeout on every waiting signature
UNREACHABLE_PAUSE = 120    # seconds


class TimestampError(RuntimeError):
    def __init__(self, msg: str, unreachable: bool = False):
        super().__init__(msg)
        self.unreachable = unreachable  # couldn't reach the service at all (vs. it answered badly)


def available() -> bool:
    try:
        import asn1crypto  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def enabled() -> bool:
    from .config import settings
    if settings.demo:  # the demo lab's signatures are fictional
        return False
    cfg = sitesettings.get("timestamps")
    return available() and bool(cfg["enabled"]) and bool(cfg["tsa_url"])


# ------------------------------------------------------------------ what gets timestamped

def payload_for(session: Session, sig: Signature) -> str:
    exp = session.get(Experiment, sig.experiment_id)
    data = {"benchtop_signature": sig.id, "record": exp.eid if exp else sig.experiment_id, "meaning": sig.meaning,
            "signer": sig.signer_username or "", "signer_name": sig.signer_name or "",
            "signed_at": sig.signed_at.replace(microsecond=0).isoformat() + "Z", "content_sha256": sig.content_hash}
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def queue_for(session: Session, sig: Signature) -> None:
    """Called in the signing transaction: the token is fetched after it commits."""
    if not enabled():
        return
    payload = payload_for(session, sig)
    row = SignatureTimestamp(signature_id=sig.id, payload=payload, digest=hashlib.sha256(payload.encode()).hexdigest(),
                             tsa_url=sitesettings.get("timestamps")["tsa_url"])
    session.add(row)
    session.flush()
    session.info.setdefault("benchtop_tsa", []).append(row.id)


# ------------------------------------------------------------------ RFC 3161 request / response

def build_request(digest: bytes, nonce: int) -> bytes:
    from asn1crypto import algos, tsp
    req = tsp.TimeStampReq({
        "version": "v1",
        "message_imprint": tsp.MessageImprint({"hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}), "hashed_message": digest}),
        "nonce": nonce, "cert_req": True,
    })
    return req.dump()


def _post(url: str, body: bytes) -> bytes:
    if transport is not None:
        return transport(url, body)
    deadline = time.monotonic() + TOTAL_SECONDS
    with httpx.stream("POST", url, content=body, timeout=20, follow_redirects=True,
                      headers={"Content-Type": "application/timestamp-query", "Accept": "application/timestamp-reply",
                               "User-Agent": f"Benchtop-ELN/{__version__}"}) as r:
        if r.status_code != 200:
            raise TimestampError(f"The timestamp service answered {r.status_code}")
        buf = bytearray()
        for chunk in r.iter_bytes():  # a token is a few KB: don't let a broken service fill memory or drip forever
            buf += chunk
            if len(buf) > MAX_REPLY:
                raise TimestampError("The timestamp service sent far more than a timestamp")
            if time.monotonic() > deadline:
                raise TimestampError("The timestamp service is too slow to answer", unreachable=True)
        return bytes(buf)


def fetch_token(url: str, digest: bytes) -> tuple[bytes, dict[str, Any]]:
    """Ask the TSA for a token on `digest`; returns (token DER, verification details)."""
    from asn1crypto import parser, tsp
    nonce = secrets.randbits(63)
    try:
        raw = _post(url, build_request(digest, nonce))
    except (httpx.HTTPError, OSError) as exc:
        raise TimestampError(f"Couldn't reach the timestamp service ({exc.__class__.__name__})", unreachable=True) from None
    try:
        # read the status on its own first: a refusal carries no token, which the full structure would reject
        _, _, _, _, contents, _ = parser.parse(raw)
        _, _, _, header, body, trailer = parser.parse(contents)
        status_info = tsp.PKIStatusInfo.load(header + body + trailer)
        status = status_info["status"].native
    except Exception as exc:  # noqa: BLE001 - garbage instead of a TimeStampResp
        raise TimestampError(f"The timestamp service sent something that isn't a timestamp ({exc.__class__.__name__})") from None
    if status not in ("granted", "granted_with_mods"):
        text = " ".join(status_info["status_string"].native or []) if status_info["status_string"].native else ""
        raise TimestampError(f"The timestamp service refused: {status} {text}".strip())
    try:
        token = tsp.TimeStampResp.load(raw)["time_stamp_token"].dump()
    except Exception as exc:  # noqa: BLE001
        raise TimestampError(f"The timestamp service's reply is incomplete ({exc.__class__.__name__})") from None
    info = verify(token, digest, nonce)
    if not info["ok"]:
        raise TimestampError(f"The timestamp didn't check out: {info['error']}")
    return token, info


def verify(token: bytes, digest: bytes, nonce: int | None = None) -> dict[str, Any]:
    """Check a TimeStampToken: it's for this digest, its content is intact, and the TSA's signature is valid.
    (It doesn't check the TSA certificate's chain to a root; `openssl ts -verify` with the system roots does.)"""
    from asn1crypto import cms, tsp  # noqa: F401 - importing tsp registers the TSTInfo content type with cms
    out: dict[str, Any] = {"ok": False, "error": "", "gen_time": None, "tsa_name": "", "serial": None,
                           "trusted": False, "cert_sha256": ""}
    try:
        ci = cms.ContentInfo.load(token)
        if ci["content_type"].native != "signed_data":
            raise TimestampError("not a signed token")
        sd = ci["content"]
        eci = sd["encap_content_info"]
        if eci["content_type"].native != "tst_info":
            raise TimestampError("not a timestamp")
        tst_der = eci["content"].contents
        tst = eci["content"].parsed
        mi = tst["message_imprint"]
        if mi["hashed_message"].native != digest:
            raise TimestampError("it is for a different fingerprint")
        if nonce is not None and tst["nonce"].native not in (None, nonce):
            raise TimestampError("the nonce doesn't match (possible replay)")
        out["gen_time"] = tst["gen_time"].native
        out["serial"] = tst["serial_number"].native
        si = sd["signer_infos"][0]
        cert = _signer_cert(sd, si)
        out["tsa_name"] = _cert_name(cert) if cert is not None else (tst["tsa"].native and str(tst["tsa"].native) or "")
        if cert is None:
            raise TimestampError("the token doesn't include the service's certificate")
        _check_signature(si, cert, tst_der)
        _valid_when_made(cert, out["gen_time"])
        eku = cert.extended_key_usage_value
        if eku is not None and "time_stamping" not in eku.native:
            raise TimestampError("the certificate isn't for timestamping")
        out["cert_sha256"] = hashlib.sha256(cert.dump()).hexdigest()
        out["trusted"] = _chain_trusted(sd, cert)
        out["ok"] = True
    except TimestampError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - malformed token
        out["error"] = f"unreadable token ({exc.__class__.__name__})"
    return out


def _valid_when_made(cert, gen_time) -> None:
    """The service's certificate must have been valid at the time it gives the timestamp (0.36.6: an expired or
    not-yet-valid one was accepted). Its expiring later doesn't matter: a timestamp made while the certificate was
    valid stays valid, as RFC 3161 and `openssl ts -verify` treat it, so old stamps don't turn bad over the years."""
    if not isinstance(gen_time, datetime):
        raise TimestampError("the timestamp has no time")
    start, end = cert["tbs_certificate"]["validity"]["not_before"].native, cert["tbs_certificate"]["validity"]["not_after"].native
    t = gen_time if gen_time.tzinfo else gen_time.replace(tzinfo=timezone.utc)
    day = "%d %b %Y"
    if t < start:
        raise TimestampError(f"the service's certificate wasn't valid yet when it made the timestamp (valid from "
                             f"{start:{day}}, timestamp {t:{day} %H:%M} UTC)")
    if t > end:
        raise TimestampError(f"the service's certificate had expired when it made the timestamp (expired "
                             f"{end:{day}}, timestamp {t:{day} %H:%M} UTC)")


_roots_cache: dict[bytes, list] | None = None


def _roots() -> dict[bytes, list]:
    """Certificate authorities trusted by browsers and operating systems (the certifi bundle), by subject."""
    global _roots_cache
    if _roots_cache is None:
        from cryptography import x509 as cx
        found: dict[bytes, list] = {}
        try:
            import certifi
            import warnings
            pem = open(certifi.where(), "rb").read()
            for block in pem.split(b"-----BEGIN CERTIFICATE-----")[1:]:
                try:  # one at a time: an oddly made root shouldn't stop the others loading
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        c = cx.load_pem_x509_certificate(b"-----BEGIN CERTIFICATE-----" + block)
                    found.setdefault(c.subject.public_bytes(), []).append(c)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 - no bundle: nothing counts as trusted
            log.warning("Couldn't load the list of trusted certificate authorities")
        _roots_cache = found
    return _roots_cache


def _chain_trusted(sd, leaf) -> bool:
    """Does the timestamp service's certificate lead, through the certificates in the token, to a well-known
    authority? (So a token re-signed with a made-up certificate doesn't pass.) Expiry isn't checked: a
    timestamp stays valid after the service's certificate expires."""
    from cryptography import x509 as cx
    try:
        pool = [cx.load_der_x509_certificate(c.chosen.dump()) for c in (sd["certificates"] or []) if c.name == "certificate"]
        cur = cx.load_der_x509_certificate(leaf.dump())
        roots = _roots()
        for _ in range(8):
            for root in roots.get(cur.issuer.public_bytes(), []):
                try:
                    cur.verify_directly_issued_by(root)
                    return True
                except Exception:  # noqa: BLE001 - try the next root with that name
                    continue
            parent = next((c for c in pool if c.subject == cur.issuer and c != cur), None)
            if parent is None:
                return False
            cur.verify_directly_issued_by(parent)
            cur = parent
    except Exception:  # noqa: BLE001 - a broken link anywhere: not trusted
        return False
    return False


def _signer_cert(sd, si):
    sid = si["sid"]
    for c in sd["certificates"] or []:
        if c.name != "certificate":
            continue
        cert = c.chosen
        if sid.name == "issuer_and_serial_number":
            if cert.issuer == sid.chosen["issuer"] and cert.serial_number == sid.chosen["serial_number"].native:
                return cert
        elif cert.key_identifier == sid.chosen.native:
            return cert
    return None


def _cert_name(cert) -> str:
    subj = cert.subject.native
    return subj.get("common_name") or subj.get("organization_name") or cert.subject.human_friendly


_HASHES = {"sha1": "SHA1", "sha224": "SHA224", "sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}


def _check_signature(si, cert, content: bytes) -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    digest_name = si["digest_algorithm"]["algorithm"].native
    if digest_name not in _HASHES:
        raise TimestampError(f"unsupported digest {digest_name}")
    h = getattr(hashes, _HASHES[digest_name])()
    attrs = si["signed_attrs"]
    if not attrs:
        signed = content
    else:
        md = next((a["values"][0].native for a in attrs if a["type"].native == "message_digest"), None)
        if md != hashlib.new(digest_name, content).digest():
            raise TimestampError("the timestamp's content was altered")
        raw = attrs.dump()  # signed as a SET (tag 0x31), not as the [0] IMPLICIT field they're stored in: same bytes
        signed = b"\x31" + raw[1:]
    key = serialization.load_der_public_key(cert.public_key.dump())
    sig = si["signature"].native
    algo = si["signature_algorithm"].signature_algo
    try:
        if isinstance(key, rsa.RSAPublicKey):
            pad = padding.PSS(mgf=padding.MGF1(h), salt_length=padding.PSS.AUTO) if algo == "rsassa_pss" else padding.PKCS1v15()
            key.verify(sig, signed, pad, h)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(sig, signed, ec.ECDSA(h))
        else:
            raise TimestampError("unsupported key type")
    except TimestampError:
        raise
    except Exception:  # noqa: BLE001 - cryptography's InvalidSignature
        raise TimestampError("the service's signature doesn't verify") from None


# ------------------------------------------------------------------ fetching in the background

def process(ts_id: int) -> None:
    from .db import engine
    from . import audit
    global _skip_until
    got = False
    with Session(engine) as s:
        row = s.get(SignatureTimestamp, ts_id)
        if row is None or row.status == "ok":
            return
        if row.attempts and time.monotonic() < _skip_until:
            return  # a retry while the service is unreachable: stays pending (a new signature still tries, and its success catches up the rest)
        row.attempts += 1
        row.updated_at = utcnow()
        # always the service chosen now: if an admin switched because the old one was down, retries go to the new one
        url = sitesettings.get("timestamps")["tsa_url"] or row.tsa_url
        try:
            token, info = fetch_token(url, bytes.fromhex(row.digest))
            row.token, row.status, row.error, row.tsa_url = base64.b64encode(token).decode(), "ok", "", url
            row.tsa_name = info["tsa_name"]
            gt = info["gen_time"]
            row.gen_time = gt.astimezone(timezone.utc).replace(tzinfo=None) if isinstance(gt, datetime) else None
            sig = s.get(Signature, row.signature_id)
            audit.record(s, None, "timestamp", "signature", row.signature_id, experiment_id=sig.experiment_id if sig else None,
                         tsa=row.tsa_name, gen_time=row.gen_time.isoformat() + "Z" if row.gen_time else None, digest=row.digest,
                         cert_sha256=info["cert_sha256"], trusted=info["trusted"])
            got = True
        except TimestampError as exc:
            row.error, row.tsa_url = str(exc), url
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            if exc.unreachable:
                _skip_until = time.monotonic() + UNREACHABLE_PAUSE
            log.info("Timestamp for signature %s not obtained yet: %s", row.signature_id, exc)
        s.add(row)
        s.commit()
        if got:  # the service works again: fetch the others that are waiting now rather than at the next hourly retry
            _skip_until = 0.0
            for other in s.exec(select(SignatureTimestamp.id).where(SignatureTimestamp.status == "pending")).all():
                _submit(other)


def _run() -> None:
    while True:
        ts_id = _q.get()
        with _lock:
            _queued.discard(ts_id)
        try:
            process(ts_id)
        except Exception:  # noqa: BLE001
            log.exception("Timestamp worker crashed")


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="benchtop-timestamps", daemon=True)
            _worker.start()


def _submit(ts_id: int) -> None:
    if synchronous:
        with _lock:
            if ts_id in _queued:
                return
            _queued.add(ts_id)
        try:
            process(ts_id)
        finally:
            with _lock:
                _queued.discard(ts_id)
        return
    with _lock:
        if ts_id in _queued:
            return
        _queued.add(ts_id)
    _q.put(ts_id)
    _ensure_worker()


def _after_commit(session: Session) -> None:
    for ts_id in session.info.pop("benchtop_tsa", None) or []:
        _submit(ts_id)


def retry_pending(include_failed: bool = False) -> int:
    """Housekeeping (hourly), or an admin's "Retry now"."""
    from .db import engine
    global _skip_until
    if not enabled():
        return 0
    _skip_until = 0.0
    with Session(engine) as s:
        statuses = ["pending", "failed"] if include_failed else ["pending"]
        rows = s.exec(select(SignatureTimestamp).where(SignatureTimestamp.status.in_(statuses))).all()
        ids = []
        for r in rows:
            if include_failed and r.status == "failed":
                r.status, r.attempts = "pending", 0
                s.add(r)
            ids.append(r.id)
        s.commit()
    for i in ids:
        _submit(i)
    return len(ids)


def out(row: SignatureTimestamp | None, signed_at: datetime | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    delay = (row.gen_time - signed_at).total_seconds() if row.gen_time and signed_at else None
    return {"status": row.status, "tsa": row.tsa_name or row.tsa_url, "gen_time": row.gen_time.isoformat() + "Z" if row.gen_time else None,
            "delay_seconds": delay, "error": row.error if row.status != "ok" else "", "attempts": row.attempts}


def test_service(url: str) -> dict[str, Any]:
    """Timestamp a random fingerprint to check a service works from here."""
    if not available():
        return {"ok": False, "error": "Restart Benchtop so it can install the timestamp packages"}
    try:
        _, info = fetch_token(url, hashlib.sha256(secrets.token_bytes(32)).digest())
        gt = info["gen_time"]
        return {"ok": True, "tsa": info["tsa_name"], "gen_time": gt.isoformat() if gt else None, "trusted": info["trusted"]}
    except TimestampError as exc:
        return {"ok": False, "error": str(exc)}


def proof_zip(session: Session, sig: Signature, row: SignatureTimestamp) -> bytes:
    """Everything needed to check a timestamp independently with openssl."""
    import io
    import zipfile
    from asn1crypto import cms, pem
    token = base64.b64decode(row.token or "")
    certs = []
    try:
        for c in cms.ContentInfo.load(token)["content"]["certificates"] or []:
            if c.name == "certificate":
                certs.append(pem.armor("CERTIFICATE", c.chosen.dump()).decode())
    except Exception:  # noqa: BLE001
        pass
    exp = session.get(Experiment, sig.experiment_id)
    readme = f"""Trusted timestamp for a Benchtop signature
==========================================

Record:     {exp.eid if exp else sig.experiment_id}  {exp.title if exp else ''}
Signature:  {sig.meaning} by {sig.signer_name or sig.signer_username} at {sig.signed_at.replace(microsecond=0).isoformat()}Z
Timestamp:  {row.gen_time.isoformat() + 'Z' if row.gen_time else 'n/a'} by {row.tsa_name or row.tsa_url}

signature.json  the exact text that was timestamped (its SHA-256 is {row.digest})
token.tst       the timestamp token from the service (RFC 3161, DER)
tsa-certs.pem   the service's certificates, as included in the token

To read the token:
    openssl ts -reply -token_in -in token.tst -text

To check it independently of Benchtop (macOS / Linux):
    openssl ts -verify -data signature.json -in token.tst -token_in \\
        -CAfile /etc/ssl/cert.pem -untrusted tsa-certs.pem
"Verification: OK" means the service certifies that this signature (record, meaning, signer, time and the
SHA-256 of the record's content, 'content_sha256') existed at the time shown. To link it to the record itself,
compare content_sha256 with the content hash shown in Benchtop's integrity check or the PDF of the record.
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("signature.json", row.payload)
        z.writestr("token.tst", token)
        z.writestr("tsa-certs.pem", "".join(certs))
        z.writestr("README.txt", readme)
    return buf.getvalue()


def _install_hooks() -> None:
    from sqlalchemy import event
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_soft_rollback", lambda s, prev: s.info.pop("benchtop_tsa", None))


_install_hooks()
