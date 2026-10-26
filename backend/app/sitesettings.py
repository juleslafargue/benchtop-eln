"""Settings an admin changes in the app (email server, trusted timestamps, witness reminders, publishing to Zenodo).

Kept in <data>/site-settings.json, next to the database, so they're in backups' data folder but not in the
database itself. Secrets (the email password) are encrypted with the server's secret key. Environment
variables, when set, take precedence and make the setting read-only in the app.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from typing import Any

from .config import settings

_lock = threading.Lock()
SECRET_KEYS = {"password"}

DEFAULTS: dict[str, dict[str, Any]] = {
    "email": {"host": "", "port": 587, "security": "starttls", "username": "", "password": "",
              "from_address": "", "from_name": "Benchtop", "base_url": ""},
    "timestamps": {"enabled": True, "tsa_url": "http://timestamp.digicert.com"},
    "witness": {"remind_after_days": 3, "overdue_after_days": 7},
    # publishing signed records on Zenodo (0.28): which Zenodo site (sandbox.zenodo.org to try it out), and a community
    "zenodo": {"enabled": True, "url": "https://zenodo.org", "community": ""},
}
# environment variables that override a setting (the app shows them as fixed)
ENV = {
    ("email", "host"): "BENCHTOP_SMTP_HOST", ("email", "port"): "BENCHTOP_SMTP_PORT",
    ("email", "security"): "BENCHTOP_SMTP_SECURITY", ("email", "username"): "BENCHTOP_SMTP_USER",
    ("email", "password"): "BENCHTOP_SMTP_PASSWORD", ("email", "from_address"): "BENCHTOP_SMTP_FROM",
    ("email", "base_url"): "BENCHTOP_PUBLIC_URL",
    ("timestamps", "enabled"): "BENCHTOP_TIMESTAMPS", ("timestamps", "tsa_url"): "BENCHTOP_TSA_URL",
    ("zenodo", "enabled"): "BENCHTOP_ZENODO", ("zenodo", "url"): "BENCHTOP_ZENODO_URL",
    ("zenodo", "community"): "BENCHTOP_ZENODO_COMMUNITY",
}


def _path():
    return settings.data_dir / "site-settings.json"


def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("benchtop-site-settings:" + settings.secret_key).encode()).digest())
    return Fernet(key)


def _encrypt(v: str) -> str:
    return "enc:" + _fernet().encrypt(v.encode()).decode() if v else ""


def _decrypt(v: str) -> str:
    if not v:
        return ""
    if not v.startswith("enc:"):
        return v
    try:
        return _fernet().decrypt(v[4:].encode()).decode()
    except Exception:  # noqa: BLE001 - secret key changed: the password has to be entered again
        return ""


def encrypt(v: str) -> str:
    """A secret kept elsewhere (someone's Zenodo token), encrypted the same way as the email password."""
    return _encrypt(v)


def decrypt(v: str) -> str:
    """"" when it can't be read (the server's secret key changed): it has to be entered again."""
    return _decrypt(v)


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _coerce(section: str, key: str, raw: Any) -> Any:
    default = DEFAULTS[section][key]
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).lower() not in ("0", "off", "false", "no", "")
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return "" if raw is None else str(raw)


def get(section: str) -> dict[str, Any]:
    """The section's settings with secrets decrypted (for use by the server, never sent to the browser)."""
    stored = _load().get(section) or {}
    out = {}
    for key, default in DEFAULTS[section].items():
        env = ENV.get((section, key))
        if env and os.environ.get(env) is not None:
            out[key] = _coerce(section, key, os.environ[env])
        elif key in stored:
            out[key] = _coerce(section, key, _decrypt(stored[key]) if key in SECRET_KEYS else stored[key])
        else:
            out[key] = default
    return out


def public(section: str) -> dict[str, Any]:
    """For the admin page: secrets replaced by whether they're set, plus which keys are fixed by the environment."""
    data = get(section)
    for k in SECRET_KEYS & data.keys():
        data[k] = ""
        data[f"{k}_set"] = bool(get(section)[k])
    data["fixed"] = [k for (s, k), env in ENV.items() if s == section and os.environ.get(env) is not None]
    return data


def update(section: str, changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        cur = data.get(section) or {}
        for key, value in changes.items():
            if key not in DEFAULTS[section]:
                continue
            if key in SECRET_KEYS:
                if value is None:  # left blank in the form: keep the stored secret
                    continue
                cur[key] = _encrypt(str(value))
            else:
                cur[key] = _coerce(section, key, value)
        data[section] = cur
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(_path())
    return public(section)
