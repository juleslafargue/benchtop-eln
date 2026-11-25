"""A fake RFC 3161 timestamp authority for tests: real tokens, signed by a throwaway certificate.

mode: "ok" | "refuse" | "garbage" | "down" | "tamper" (content changed after signing) | "wrong_digest"
"""
from __future__ import annotations

import datetime as dt
import hashlib

from asn1crypto import algos, cms, core, tsp, x509 as ax509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tests")])


def _issue(subject: x509.Name, key, issuer: x509.Name, issuer_key, *, ca: bool, tsa: bool,
           valid: tuple[int, int] = (-1, 365)) -> x509.Certificate:
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now + dt.timedelta(days=valid[0]))
         .not_valid_after(now + dt.timedelta(days=valid[1]))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if tsa:
        b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
    return b.sign(issuer_key, hashes.SHA256())


class FakeTSA:
    """chain=False: a self-signed service (like FreeTSA, not from a well-known authority).
    chain=True: issued by a test root through an intermediate, like DigiCert; `root` is that root."""

    def __init__(self, name: str = "Benchtop Test TSA", chain: bool = False, valid: tuple[int, int] = (-1, 365),
                 stamped_days_ago: float = 0):
        """valid: the service certificate's first and last day, in days from now (0.36.6: expired or not yet valid
        ones). stamped_days_ago: the time the service writes in its timestamps, as if it had made them then."""
        self.stamped_days_ago = stamped_days_ago
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.extra: list[ax509.Certificate] = []
        self.root = None
        if chain:
            root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            mid_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.root = _issue(_name("Test Root CA"), root_key, _name("Test Root CA"), root_key, ca=True, tsa=False)
            mid = _issue(_name("Test Timestamping CA"), mid_key, self.root.subject, root_key, ca=True, tsa=False)
            self.cert = _issue(_name(name), self.key, mid.subject, mid_key, ca=False, tsa=True, valid=valid)
            self.extra = [ax509.Certificate.load(mid.public_bytes(serialization.Encoding.DER))]
        else:
            self.cert = _issue(_name(name), self.key, _name(name), self.key, ca=True, tsa=True, valid=valid)
        self.cert_der = self.cert.public_bytes(serialization.Encoding.DER)
        self.acert = ax509.Certificate.load(self.cert_der)
        self.mode = "ok"
        self.serial = 1000
        self.requests = 0

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def __call__(self, url: str, body: bytes) -> bytes:
        self.requests += 1
        if self.mode == "down":
            raise OSError("connection refused")
        if self.mode == "garbage":
            return b"<html>Service unavailable</html>"
        req = tsp.TimeStampReq.load(body)
        if self.mode == "refuse":
            status = tsp.PKIStatusInfo({"status": "rejection", "status_string": ["policy not supported"]}).dump()
            return b"\x30" + bytes([len(status)]) + status  # a TimeStampResp holding only the status, as real refusals do
        imprint = req["message_imprint"]
        if self.mode == "wrong_digest":
            imprint = tsp.MessageImprint({"hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}), "hashed_message": b"\x00" * 32})
        self.serial += 1
        tst = tsp.TSTInfo({"version": "v1", "policy": "1.2.3.4.1", "message_imprint": imprint, "serial_number": self.serial,
                           "gen_time": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.stamped_days_ago)).replace(microsecond=0), "nonce": req["nonce"].native})
        tst_der = tst.dump()
        ess = tsp.SigningCertificateV2({"certs": [tsp.ESSCertIDv2({"hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
                                                                   "cert_hash": hashlib.sha256(self.cert_der).digest()})]})
        attrs = cms.CMSAttributes([
            cms.CMSAttribute({"type": "content_type", "values": ["tst_info"]}),
            cms.CMSAttribute({"type": "message_digest", "values": [hashlib.sha256(tst_der).digest()]}),
            cms.CMSAttribute({"type": "signing_certificate_v2", "values": [ess]}),
        ])
        signature = self.key.sign(attrs.dump(), padding.PKCS1v15(), hashes.SHA256())
        if self.mode == "tamper":  # the content changes after the TSA signed it
            tst = tsp.TSTInfo({**tst.native, "gen_time": dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc),
                               "message_imprint": req["message_imprint"].native})
            tst_der = tst.dump()
        si = cms.SignerInfo({
            "version": "v1",
            "sid": cms.SignerIdentifier({"issuer_and_serial_number": cms.IssuerAndSerialNumber(
                {"issuer": self.acert.issuer, "serial_number": self.acert.serial_number})}),
            "digest_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
            "signed_attrs": attrs,
            "signature_algorithm": algos.SignedDigestAlgorithm({"algorithm": "sha256_rsa"}),
            "signature": signature,
        })
        sd = cms.SignedData({
            "version": "v3", "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
            "encap_content_info": {"content_type": "tst_info", "content": core.ParsableOctetString(tst_der)},
            "certificates": [self.acert, *self.extra], "signer_infos": [si],
        })
        token = cms.ContentInfo({"content_type": "signed_data", "content": sd})
        return tsp.TimeStampResp({"status": {"status": "granted"}, "time_stamp_token": token}).dump()
