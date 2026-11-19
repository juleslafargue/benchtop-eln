"""Is there a newer Benchtop?

Once a day, while Benchtop runs, this looks at the list of releases on GitHub and remembers the ones newer than
this copy, so the admin's dashboard can say so. It never downloads or installs anything by itself: updating is
always a person running ./benchtop update (or deploy/update-server.sh on a lab server), which backs up first.
BENCHTOP_UPDATE_CHECK=off stops the daily look (the only thing it sends GitHub is an ordinary web request).

Command line (used by ./benchtop update):
  python -m app.updates check    what's newer than this copy, with the release notes
  python -m app.updates zip      three lines for the newest release: version, zip download link, zip SHA-256
"""
from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import settings

log = logging.getLogger("benchtop.updates")
CHECK_EVERY = timedelta(hours=23)  # "daily", without drifting later each day
NOTES_MAX = 6000
ZIP_RE = re.compile(r"^benchtop-eln-v\d+(\.\d+)*\.zip$")


def parse_version(v: Any) -> Optional[tuple[int, ...]]:
    """'v0.18.3' or '0.18.3' -> (0, 18, 3); anything else (pre-releases, typos) -> None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(candidate: str, than: Optional[str] = None) -> bool:
    a, b = parse_version(candidate), parse_version(than if than is not None else __version__)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _release(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    if r.get("draft") or r.get("prerelease"):
        return None
    version = str(r.get("tag_name") or "").lstrip("v")
    if parse_version(version) is None:
        return None
    zips = [a for a in r.get("assets") or [] if ZIP_RE.match(str(a.get("name") or ""))]
    digest = str(zips[0].get("digest") or "") if zips else ""
    return {
        "version": version,
        "name": r.get("name") or f"Benchtop {version}",
        "url": r.get("html_url") or "",
        "published_at": r.get("published_at") or "",
        "notes": str(r.get("body") or "")[:NOTES_MAX],
        "zip_url": str(zips[0].get("browser_download_url") or "") if zips else "",
        "zip_sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
    }


def fetch_releases(url: Optional[str] = None, timeout: float = 15) -> list[dict[str, Any]]:
    """Published releases, newest version first. Raises OSError/ValueError when GitHub can't be reached or answers oddly."""
    url = url or settings.update_url
    if url.startswith(("http://", "https://")) and "?" not in url:
        url += "?per_page=30"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": f"Benchtop-ELN/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL, or a file:// one set by the admin
        data = json.loads(resp.read(5_000_000))
    if not isinstance(data, list):
        raise ValueError("GitHub's answer wasn't a list of releases")
    out = [x for x in (_release(r) for r in data if isinstance(r, dict)) if x]
    out.sort(key=lambda x: parse_version(x["version"]) or (), reverse=True)
    return out


def _path() -> Path:
    return settings.data_dir / "update-check.json"


def _state() -> dict[str, Any]:
    try:
        s = json.loads(_path().read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(p)
    except OSError as e:  # a full disk mustn't stop Benchtop; the next check tries again
        log.warning("Couldn't save the update check: %s", e)


def check(now: Optional[datetime] = None) -> dict[str, Any]:
    """Look now and remember the answer. Keeps the last good answer if GitHub can't be reached."""
    now = now or datetime.now(timezone.utc)
    state = _state()
    try:
        newer = [r for r in fetch_releases() if is_newer(r["version"])]
        state = {"checked_at": now.isoformat(), "releases": newer[:10], "error": None}
    except Exception as e:  # noqa: BLE001 - offline, rate-limited, GitHub down: try again tomorrow
        state.update(checked_at=now.isoformat(), error=f"{type(e).__name__}: {e}"[:300])
    _save(state)
    return status()


def maybe_check(now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Housekeeping: at most once a day, and never in the demo or when switched off."""
    if not settings.update_check or settings.demo:
        return None
    now = now or datetime.now(timezone.utc)
    last = _state().get("checked_at")
    try:
        if last and datetime.fromisoformat(last) > now - CHECK_EVERY:
            return None
    except ValueError:
        pass
    return check(now)


def how_to_update() -> dict[str, str]:
    kind = settings.install_kind
    if kind == "docker":
        return {"kind": kind, "command": "deploy/update-server.sh",
                "where": "on the server, in the Benchtop folder"}
    if kind in ("git", "zip"):
        return {"kind": kind, "command": "./benchtop update",
                "where": "in Terminal, in the Benchtop folder, after closing Benchtop's window"}
    return {"kind": "", "command": "", "where": ""}


def status() -> dict[str, Any]:
    """What the admin sees: the newest release newer than this copy, if any (re-checked against this copy's
    version, so the notice disappears as soon as Benchtop has been updated)."""
    s = _state()
    newer = [r for r in s.get("releases") or [] if isinstance(r, dict) and is_newer(r.get("version", ""))]
    latest = newer[0] if newer else None
    return {
        "enabled": settings.update_check and not settings.demo,
        "current": __version__,
        "available": latest and {k: latest.get(k, "") for k in ("version", "name", "url", "published_at")},
        "versions_behind": len(newer),
        "checked_at": s.get("checked_at"),
        "error": s.get("error"),
        **how_to_update(),
    }


CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain(text: str) -> str:
    """Release notes are other people's text: without control characters, which could make the terminal do things
    (clear it, retitle it, colour everything) when ./benchtop update shows them (0.36.3). Newlines and tabs stay."""
    return CONTROLS.sub("", text.replace("\r\n", "\n"))


def why_not(e: BaseException) -> str:
    """What went wrong looking for a newer version, in words that say what to do."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            said = str(json.loads(e.read(20000) or b"{}").get("message") or "")
        except (OSError, ValueError, AttributeError):
            said = ""
        if e.code in (403, 429) and ("rate limit" in said.lower() or (e.headers or {}).get("X-RateLimit-Remaining") == "0"):
            return ("GitHub is limiting how often this computer can ask for new versions (its rate limit). "
                    "Try again in an hour.")
        return f"GitHub answered with an error ({e.code} {plain(said or str(e.reason))[:200]}). Try again later."
    if isinstance(e, ValueError):  # not JSON, or not a list of releases
        return f"GitHub's list of releases couldn't be read ({plain(str(e))[:200]}). Try again later."
    return f"Couldn't reach GitHub to look for a newer version ({plain(str(e))[:200]}). Check the internet connection and try again."


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    try:
        releases = fetch_releases()
    except Exception as e:  # noqa: BLE001
        print(why_not(e), file=sys.stderr)
        return 3
    newer = [r for r in releases if is_newer(r["version"])]
    if cmd == "zip":
        with_zip = [r for r in newer if r["zip_url"]]
        if not with_zip:
            return 1
        r = with_zip[0]
        print(f"{r['version']}\n{r['zip_url']}\n{r['zip_sha256']}")
        return 0
    if cmd == "check":
        if not newer:
            print(f"You have the newest version ({__version__}).")
            return 1
        print(f"Benchtop {newer[0]['version']} is available (you have {__version__}).")
        for r in reversed(newer[:10]):  # oldest first, so it reads as a story
            changes = re.split(r"\n-{3,}\s*\n", "\n" + plain(r["notes"]), maxsplit=1)[0].strip()  # without "how to install"
            print(f"\n== {plain(str(r['name']))} ==\n{changes or '(no notes)'}")
        if len(newer) > 10:
            print(f"\n…and {len(newer) - 10} older releases: {plain(str(newer[-1]['url'])).rsplit('/tag/', 1)[0]}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
