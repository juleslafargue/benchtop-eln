"""Pictures a browser can't show, such as TIFF from a microscope, gel imager or scanner: a PNG to look at (0.36).

A preview is for display only. It is made from the stored file when it's asked for, kept for a while in memory, never
stored with the record and never part of what a signature covers: the record holds the file itself, its name and its
SHA-256, all unchanged. A file that can't be read, or is too big to decode, gets no preview, said plainly.
The PDF needs none: its renderer draws TIFF itself.
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
import warnings
from collections import OrderedDict
from pathlib import Path

TIFF_TYPES = {"image/tiff", "image/x-tiff", "image/tif"}
SIDE = 1600                  # the longest side of a preview, in pixels
MAX_FILE = 200 * 1024 * 1024  # a bigger file isn't read for a preview
MAX_PIXELS = 60_000_000       # nor one with more pixels than this (about 7 700 × 7 700): decoding it takes too much memory
_CACHE: OrderedDict[str, tuple[bytes, int, bool]] = OrderedDict()
_CACHED = 8
_lock = threading.Lock()


class PreviewError(ValueError):
    """Why there's no preview, in a few plain words."""


class Mismatch(ValueError):
    """The stored file no longer matches its SHA-256."""


def is_tiff(content_type: str, filename: str) -> bool:
    return (content_type or "").lower() in TIFF_TYPES or bool(re.search(r"\.tiff?$", filename or "", re.I))


def _displayable(im):
    """The picture in a mode PNG holds and a screen shows. 16-bit and floating-point pictures (common from microscopes)
    are scaled from their own darkest to their lightest value, as image viewers do: True says it was."""
    if im.mode in ("1", "L", "LA", "RGB", "RGBA"):
        return im, False
    if im.mode == "P":
        return im.convert("RGBA"), False
    if im.mode.startswith("I") or im.mode == "F":
        im = im.convert("F") if im.mode == "F" else im.convert("I")
        lo, hi = im.getextrema()
        if hi > lo:
            k = 255.0 / (hi - lo)
            im = im.point(lambda v: v * k - lo * k + 0.5)  # rounded, not cut down
        return im.convert("L"), True
    return im.convert("RGB"), False


def png(source: bytes | Path, side: int = SIDE) -> tuple[bytes, int, bool]:
    """(the PNG, how many pages the file has, whether brightness was scaled). The first page is shown."""
    from PIL import Image, ImageOps
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as im:
                w, h = im.size
                if w * h > MAX_PIXELS:
                    raise PreviewError(f"it's too large ({w:,} × {h:,} pixels)".replace(",", " "))
                pages = max(1, int(getattr(im, "n_frames", 1) or 1))
                im.seek(0)
                im.load()
                frame, scaled = _displayable(im)
                try:
                    frame = ImageOps.exif_transpose(frame)  # a picture taken on its side is shown upright
                except Exception:  # noqa: BLE001 - an odd orientation tag: shown as stored
                    pass
                frame.thumbnail((side, side))
                out = io.BytesIO()
                frame.save(out, "PNG")
                return out.getvalue(), pages, scaled
    except PreviewError:
        raise
    except Exception:  # noqa: BLE001 - a damaged or unusual file (Pillow raises many kinds of error): no preview
        raise PreviewError("it couldn't be read") from None


def for_file(path: Path, sha256: str, size: int) -> tuple[bytes, int, bool]:
    """The preview of a stored file, checked against its SHA-256 first (as a download is)."""
    with _lock:
        hit = _CACHE.get(sha256)
        if hit is not None:
            _CACHE.move_to_end(sha256)
            return hit
    if size > MAX_FILE:
        raise PreviewError(f"it's too large ({size / 1e6:.0f} MB)")
    h = hashlib.sha256()
    with open(path, "rb") as f:  # in pieces, as a download is checked
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        raise Mismatch(sha256)
    out = png(path)  # stored files never change (they're named by their SHA-256)
    with _lock:
        _CACHE[sha256] = out
        while len(_CACHE) > _CACHED:
            _CACHE.popitem(last=False)
    return out
