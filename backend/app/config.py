"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _secret_key(path: Path) -> str:
    """The key that signs sign-ins, made on the first start. Written in full before it gets its name: before 0.24.2,
    Benchtop stopped at the wrong moment of its very first start left an empty key, so sign-ins could be forged. A key
    that short is replaced (everyone signs in again)."""
    try:
        key = path.read_text().strip()
    except FileNotFoundError:
        key = ""
    if len(key) >= 32:
        return key
    tmp = path.with_name(f".secret_key.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(secrets.token_hex(32))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    if path.exists():
        os.replace(tmp, path)
    else:
        try:
            os.link(tmp, path)  # never over a key another process made at the same moment: theirs is used
        except FileExistsError:
            pass
        except OSError:  # file systems without hard links
            os.replace(tmp, path)
        tmp.unlink(missing_ok=True)
    return path.read_text().strip()


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("BENCHTOP_DATA_DIR", "./data")).resolve()
        self.database_url = os.environ.get(
            "BENCHTOP_DATABASE_URL", f"sqlite:///{self.data_dir / 'benchtop.db'}"
        )
        self.attachments_dir = self.data_dir / "attachments"
        secret_file = self.data_dir / ".secret_key"
        env_secret = os.environ.get("BENCHTOP_SECRET_KEY")
        if env_secret:
            self.secret_key = env_secret
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.secret_key = _secret_key(secret_file)
        self.token_ttl_hours = int(os.environ.get("BENCHTOP_TOKEN_TTL_HOURS", "12"))
        self.admin_username = os.environ.get("BENCHTOP_ADMIN_USER", "admin")
        self.admin_password = os.environ.get("BENCHTOP_ADMIN_PASSWORD", "admin")
        self.static_dir = os.environ.get("BENCHTOP_STATIC_DIR")  # built frontend
        self.max_upload_mb = int(os.environ.get("BENCHTOP_MAX_UPLOAD_MB", "50"))
        # instrument folders (0.16): how often to look, and the largest arrival taken in
        # set (by docker-compose.server.yml) on servers whose deploy/.env still names a Postgres password: they kept
        # their records in Postgres before 0.18.2, so an empty SQLite database there means "wrong setup", not "new lab"
        self.legacy_postgres = bool(os.environ.get("BENCHTOP_LEGACY_POSTGRES", "").strip())
        self.instrument_scan_seconds = int(os.environ.get("BENCHTOP_INSTRUMENT_SCAN_SECONDS", "60"))
        self.instrument_max_mb = int(os.environ.get("BENCHTOP_INSTRUMENT_MAX_MB", "2048"))
        # .eln import (0.20): the largest file accepted, in MB (each file inside it is also held to instrument_max_mb)
        self.import_max_mb = int(os.environ.get("BENCHTOP_IMPORT_MAX_MB", "20480"))
        # Backups: point BENCHTOP_BACKUP_DIR at an iCloud/Dropbox folder to keep copies off-machine
        self.backup_dir = Path(os.environ.get("BENCHTOP_BACKUP_DIR", self.data_dir / "backups")).expanduser().resolve()
        self.backup_keep = int(os.environ.get("BENCHTOP_BACKUP_KEEP", "14"))
        self.backup_interval_hours = float(os.environ.get("BENCHTOP_BACKUP_INTERVAL_HOURS", "24"))
        # the daily check of the live data (0.24): database, audit trail, signatures, timestamps, attached files; 0 = off
        self.check_interval_hours = float(os.environ.get("BENCHTOP_CHECK_INTERVAL_HOURS", "24"))
        obs = os.environ.get("BENCHTOP_OBSIDIAN_DIR")
        self.obsidian_dir = Path(obs).expanduser().resolve() if obs else None  # hourly Markdown sync target
        self.port = int(os.environ.get("BENCHTOP_PORT", "8000"))
        # Safety data from PubChem (public GHS classifications); set BENCHTOP_PUBCHEM=off to work fully offline
        self.pubchem_enabled = os.environ.get("BENCHTOP_PUBCHEM", "on").lower() not in ("0", "off", "false", "no")
        # Paper details for DOIs in References sections, from doi.org; set BENCHTOP_DOI=off to work fully offline
        self.doi_enabled = os.environ.get("BENCHTOP_DOI", "on").lower() not in ("0", "off", "false", "no")
        self.safety_refresh_days = float(os.environ.get("BENCHTOP_SAFETY_REFRESH_DAYS", "30"))
        # NMR shift table: re-check NMRShiftDB2 monthly and rebuild when it publishes a newer export
        self.nmr_auto_update = os.environ.get("BENCHTOP_NMR_AUTO_UPDATE", "on").lower() not in ("0", "off", "false", "no")
        # Newer versions: look at Benchtop's GitHub releases once a day and tell the admin (never installs anything).
        # BENCHTOP_UPDATE_URL points the check at a fork's releases instead.
        self.update_check = os.environ.get("BENCHTOP_UPDATE_CHECK", "on").lower() not in ("0", "off", "false", "no")
        self.update_url = os.environ.get("BENCHTOP_UPDATE_URL", "https://api.github.com/repos/juleslafargue/benchtop-eln/releases")
        # how this copy was installed (set by ./benchtop and the Docker image), so the notice names the right command
        self.install_kind = os.environ.get("BENCHTOP_INSTALL", "")
        # Single sign-on (OpenID Connect): Microsoft Entra ID / Microsoft 365, Google Workspace, Okta,
        # Keycloak, a university IdP… Register Benchtop there with the redirect URI
        # <public URL>/api/auth/sso/callback and set these three to switch it on.
        self.oidc_issuer = os.environ.get("BENCHTOP_OIDC_ISSUER", "").rstrip("/")
        self.oidc_client_id = os.environ.get("BENCHTOP_OIDC_CLIENT_ID", "")
        self.oidc_client_secret = os.environ.get("BENCHTOP_OIDC_CLIENT_SECRET", "")
        self.oidc_name = os.environ.get("BENCHTOP_OIDC_NAME", "")  # button label; guessed from the issuer when empty
        self.oidc_domains = [d.strip().lower().lstrip("@") for d in os.environ.get("BENCHTOP_OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()]
        self.oidc_auto_create = os.environ.get("BENCHTOP_OIDC_AUTO_CREATE", "on").lower() not in ("0", "off", "false", "no")
        self.oidc_default_role = os.environ.get("BENCHTOP_OIDC_DEFAULT_ROLE", "scientist")
        self.password_login = os.environ.get("BENCHTOP_PASSWORD_LOGIN", "on").lower() not in ("0", "off", "false", "no")
        self.public_url = os.environ.get("BENCHTOP_PUBLIC_URL", "").rstrip("/")  # e.g. https://eln.mylab.ca
        self.lan_url = os.environ.get("BENCHTOP_LAN_URL", "").rstrip("/")  # set by ./benchtop lab: this computer's address on the network
        # demo mode (./benchtop demo): the sign-in page lists the demo lab's accounts; set only for demo data
        self.demo = os.environ.get("BENCHTOP_DEMO", "").lower() in ("1", "on", "true", "yes")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
