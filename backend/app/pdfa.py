"""Archival PDFs (0.34): every PDF Benchtop makes is a PDF/A-3b file.

PDF/A is the ISO standard for documents kept for decades: everything needed to show the file is inside it, so it
looks the same in any reader, now and later. Benchtop's PDFs are laid out by xhtml2pdf (on ReportLab) as before;
this module then makes them archival:

- Fonts: every piece of text is set in DejaVu Sans / DejaVu Sans Mono, embedded in the file (data/pdfa/, with its
  licence). ReportLab's 14 built-in names (Helvetica, Times, Courier…), which would otherwise be referred to without
  being embedded, are pointed at the same files, so no text can fall back to a font that isn't in the file.
- Colour: an sRGB output intent (data/pdfa/sRGB-v2-micro.icc, CC0) says what the colours mean.
- Metadata: XMP with pdfaid part 3, conformance B, matching the document information (title, author, dates).
- Attachments (PDF/A-3 "associated files"): the record's own data and its timestamp proofs, each declared with its
  type and its relationship to the document.
- No encryption, no JavaScript, a document ID; links marked to print.

Only the PDF file is made here; nothing about a record, and nothing a signature covers, is read or changed.
pikepdf (qpdf) does the PDF/A part; tests/pdfa_checks.py checks the result, and GitHub's tests run veraPDF on it.
"""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

ASSETS = Path(__file__).parent / "data" / "pdfa"
ICC_PROFILE = ASSETS / "sRGB-v2-micro.icc"
SANS, MONO = "DejaVuSans", "DejaVuSansMono"
FONT_FILES = {
    "DejaVuSans": "DejaVuSans.ttf", "DejaVuSans-Bold": "DejaVuSans-Bold.ttf",
    "DejaVuSans-Oblique": "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique": "DejaVuSans-BoldOblique.ttf",
    "DejaVuSansMono": "DejaVuSansMono.ttf", "DejaVuSansMono-Bold": "DejaVuSansMono-Bold.ttf",
}
# ReportLab's built-in names, which it would write into the file without the font itself: each drawn with DejaVu
_BUILTIN = {
    "Helvetica": "DejaVuSans", "Helvetica-Bold": "DejaVuSans-Bold", "Helvetica-Oblique": "DejaVuSans-Oblique",
    "Helvetica-BoldOblique": "DejaVuSans-BoldOblique",
    "Times-Roman": "DejaVuSans", "Times-Bold": "DejaVuSans-Bold", "Times-Italic": "DejaVuSans-Oblique",
    "Times-BoldItalic": "DejaVuSans-BoldOblique",
    "Courier": "DejaVuSansMono", "Courier-Bold": "DejaVuSansMono-Bold", "Courier-Oblique": "DejaVuSansMono",
    "Courier-BoldOblique": "DejaVuSansMono-Bold",
    "Symbol": "DejaVuSans", "ZapfDingbats": "DejaVuSans",
}
_ready = False
_lock = threading.Lock()
_glyphs: set[int] = set()


class PdfError(Exception):
    """The PDF couldn't be made; the message says why, in plain words."""


def _setup() -> None:
    """Register the embedded fonts with ReportLab and xhtml2pdf, once per process."""
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from xhtml2pdf import default as x2p_default

        for name, file in FONT_FILES.items():
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / file)))
        for name, target in _BUILTIN.items():
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / FONT_FILES[target])))
        # which face is bold / italic. Registering a TrueType font maps all four styles of its own name to itself, which
        # leaves ReportLab reading "Helvetica" as bold italic: put the families back. Italic first, so a face used for
        # two styles (the monospaced one has no italic) reads as upright.
        for fam, (r, b, i, bi) in {
                SANS: ("DejaVuSans", "DejaVuSans-Bold", "DejaVuSans-Oblique", "DejaVuSans-BoldOblique"),
                MONO: ("DejaVuSansMono", "DejaVuSansMono-Bold", "DejaVuSansMono", "DejaVuSansMono-Bold"),
                "Helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
                "Times": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
                "Courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
                "Symbol": ("Symbol",) * 4, "ZapfDingbats": ("ZapfDingbats",) * 4}.items():
            for bold, italic, face in ((0, 1, i), (1, 1, bi), (0, 0, r), (1, 0, b)):
                addMapping(fam, bold, italic, face)
        # the names a stylesheet may use for a family (xhtml2pdf copies this table into each document)
        for alias in ("dejavu sans", "dejavusans", "sans-serif", "sans", "sansserif", "system-ui", "ui-sans-serif", "arial",
                      "helvetica", "verdana", "geneva", "serif", "times", "times new roman", "georgia", "ui-serif"):
            x2p_default.DEFAULT_FONT[alias] = SANS
        for alias in ("dejavu sans mono", "dejavusansmono", "monospace", "mono", "monospaced", "courier", "courier new",
                      "ui-monospace"):
            x2p_default.DEFAULT_FONT[alias] = MONO
        from reportlab.pdfbase.ttfonts import TTFontFile
        # characters every sans face has (the oblique faces lack some Arabic and N'Ko, for example)
        faces = [set(TTFontFile(str(ASSETS / FONT_FILES[f])).charToGlyph) for f in FONT_FILES if f.startswith("DejaVuSans-") or f == SANS]
        _glyphs.update(set.intersection(*faces))
        _ready = True


def font_css() -> str:
    """The stylesheet lines that put every element in the embedded fonts."""
    # code in people's text stays in the sans face, which has far more characters; the monospaced face is for
    # Benchtop's own fingerprints and commands (plain ASCII)
    return (f"body, p, td, th, li, div, span, h1, h2, h3, h4, h5, h6, a, b, i, em, strong, sub, sup, blockquote, "
            f"code, pre, tt, kbd, samp {{font-family: {SANS}}} .mono, code.mono, pre.mono {{font-family: {MONO}}}")


_UNSHOWN = re.compile(r"[^\x00-\x7f]")


def printable(html: str) -> str:
    """Characters the font hasn't got (emoji, Chinese…) as □, so no text is drawn with nothing behind it. The record's
    own data, attached inside the PDF, keeps the exact text."""
    _setup()
    return _UNSHOWN.sub(lambda m: m.group(0) if ord(m.group(0)) in _glyphs or 0xFE00 <= ord(m.group(0)) <= 0xFE0F
                        else "□", html).replace("️", "").replace("︎", "")


def html_to_pdf(html: str) -> bytes:
    """Lay out an HTML page (xhtml2pdf) with the embedded fonts; not yet PDF/A (see make_pdfa)."""
    _setup()
    try:
        from xhtml2pdf import pisa
    except ImportError:
        raise PdfError("PDF export needs one more package: stop Benchtop and start it again so it can install it") from None
    out = io.BytesIO()
    try:
        res = pisa.CreatePDF(printable(html), dest=out, encoding="utf-8")
    except RecursionError:
        raise PdfError("This record's formatting is too complex for a PDF; the printable report (Print) still works") from None
    if res.err:
        raise PdfError("Couldn't make the PDF; the printable report (Print) still works")
    return out.getvalue()


@dataclass
class Attachment:
    """A file carried inside the PDF (a PDF/A-3 associated file)."""
    name: str
    data: bytes
    mime: str
    description: str
    relationship: str = "Source"  # Source, Data, Supplement, Alternative or Unspecified


def _pdf_date(d: datetime) -> str:
    d = d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.strftime("D:%Y%m%d%H%M%S+00'00'")


def _pikepdf():
    try:
        import pikepdf
    except ImportError:
        raise PdfError("PDF export needs one more package (pikepdf): stop Benchtop and start it again so it can install it") from None
    return pikepdf


def make_pdfa(raw: bytes, *, title: str, author: str = "", subject: str = "", keywords: str = "",
              created: datetime | None = None, attachments: list[Attachment] | tuple = (), pdf=None) -> bytes:
    """The PDF in `raw` (or the open pikepdf.Pdf `pdf`) as PDF/A-3b: metadata, output intent, attachments."""
    pikepdf = _pikepdf()
    from pikepdf import Array, Dictionary, Name, String

    pdf = pdf if pdf is not None else pikepdf.open(io.BytesIO(raw))
    created = created or datetime.now(timezone.utc)
    if pdf.is_encrypted:
        raise PdfError("An encrypted PDF can't be archival")
    root = pdf.Root
    for key in ("/OpenAction", "/AA", "/JavaScript"):
        if key in root:
            del root[key]
    if "/Names" in root and "/JavaScript" in root.Names:
        del root.Names.JavaScript
    for page in pdf.pages:
        if "/AA" in page.obj:
            del page.obj.AA
        for annot in page.obj.get("/Annots", []):
            annot.F = 4  # print it, never hidden (PDF/A)
            act = annot.get("/A")
            if act is not None and act.get("/S") in (Name.JavaScript, Name.Launch):
                del annot.A
    # colour: sRGB
    icc = pdf.make_stream(ICC_PROFILE.read_bytes())
    icc.N = 3
    root.OutputIntents = Array([Dictionary(
        Type=Name.OutputIntent, S=Name.GTS_PDFA1, OutputConditionIdentifier=String("sRGB IEC61966-2.1"),
        RegistryName=String("http://www.color.org"), Info=String("sRGB IEC61966-2.1"), DestOutputProfile=icc)])
    # files carried inside, each tied to the document (PDF/A-3 associated files)
    specs = []
    when = _pdf_date(created)
    for a in attachments:
        spec = pikepdf.AttachedFileSpec(pdf, a.data, description=a.description, filename=a.name, mime_type=a.mime,
                                        creation_date=when, mod_date=when)
        spec.relationship = Name("/" + a.relationship)
        pdf.attachments[a.name] = spec
        specs.append(spec.obj)
    if specs:
        root.AF = Array(specs)
    # metadata: XMP (with the PDF/A claim) and the document information dictionary, saying the same
    for k in list(pdf.docinfo.keys()):
        del pdf.docinfo[k]
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as meta:
        meta["dc:title"] = title or "Benchtop record"
        if author:
            meta["dc:creator"] = [author]
        if subject:
            meta["dc:description"] = subject
        if keywords:
            meta["pdf:Keywords"] = keywords
        meta["pdf:Producer"] = f"Benchtop ELN {__version__} (xhtml2pdf, ReportLab, pikepdf)"
        meta["xmp:CreatorTool"] = f"Benchtop ELN {__version__}"
        stamp = created.astimezone(timezone.utc).replace(microsecond=0).isoformat() if created.tzinfo \
            else created.replace(microsecond=0).isoformat() + "+00:00"
        meta["xmp:CreateDate"] = stamp
        meta["xmp:ModifyDate"] = stamp
        meta["xmp:MetadataDate"] = stamp
        meta["pdfaid:part"] = "3"
        meta["pdfaid:conformance"] = "B"
    root.MarkInfo = Dictionary(Marked=False)
    if "/Lang" not in root:
        root.Lang = String("en")
    out = io.BytesIO()
    pdf.save(out, fix_metadata_version=False, deterministic_id=False, min_version="1.7")
    return out.getvalue()


def fonts_in(pdf):
    """Every font dictionary the pages use, including those inside forms (an overlay, say), each once."""
    seen: set = set()

    def walk(res):
        if res is None:
            return
        for _, f in res.get("/Font", {}).items():
            if f.objgen not in seen:
                seen.add(f.objgen)
                yield f
        for _, x in res.get("/XObject", {}).items():
            if x.get("/Subtype") == "/Form" and x.objgen not in seen:
                seen.add(x.objgen)
                yield from walk(x.get("/Resources"))

    for page in pdf.pages:
        yield from walk(page.obj.get("/Resources"))


def rename_subsets(pdf) -> None:
    """Give each embedded font subset its own name. Pages laid out separately each start at ReportLab's first subset
    name (AAAAAA+DejaVuSans); in one file, readers could mix up two different subsets with the same name."""
    from pikepdf import Name

    for n, f in enumerate([f for f in fonts_in(pdf) if "/BaseFont" in f]):
        base = str(f.BaseFont)[1:]
        tag = "".join(chr(65 + (n // 26 ** k) % 26) for k in range(6))
        new = f"{tag}+{base.split('+', 1)[-1]}"
        f.BaseFont = Name("/" + new)
        fd = f.get("/FontDescriptor")
        if fd is not None:
            fd.FontName = Name("/" + new)
