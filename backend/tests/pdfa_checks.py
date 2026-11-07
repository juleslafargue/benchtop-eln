"""What can be checked of a PDF/A-3b file without veraPDF (0.34): with pikepdf, the parts Benchtop is responsible for.

GitHub's tests also run veraPDF, the reference PDF/A validator, on PDFs made by the browser check (see
.github/workflows/tests.yml). This catches the same kinds of mistake earlier, anywhere the tests run."""
from __future__ import annotations

import io
import re
import struct
from typing import Any

import pikepdf
from pikepdf import Name


def _fonts(pdf: pikepdf.Pdf):
    """Every font the pages draw with, including inside forms and annotation appearances, each once."""
    seen: set = set()

    def walk(res):
        if not isinstance(res, pikepdf.Dictionary):
            return
        for _, f in res.get("/Font", {}).items():
            if f.objgen not in seen:
                seen.add(f.objgen)
                yield f
                for d in f.get("/DescendantFonts", []):
                    yield d
        for _, x in res.get("/XObject", {}).items():
            if x.get("/Subtype") == Name.Form and x.objgen not in seen:
                seen.add(x.objgen)
                yield from walk(x.get("/Resources"))

    for page in pdf.pages:
        yield from walk(page.obj.get("/Resources"))
        for a in page.obj.get("/Annots", []):
            ap = a.get("/AP")
            if ap is not None:
                for _, s in ap.items():
                    if isinstance(s, pikepdf.Stream):
                        yield from walk(s.get("/Resources"))


def used_codes(pdf: pikepdf.Pdf) -> dict[tuple, set[int]]:
    """For each simple font (by object), the character codes the content streams draw with it."""
    used: dict[tuple, set[int]] = {}
    done: set = set()

    def walk(owner, res):
        if not isinstance(res, pikepdf.Dictionary):
            return
        fonts = res.get("/Font", {})
        cur = None
        for operands, op in pikepdf.parse_content_stream(owner):
            o = str(op)
            if o == "Tf":
                f = fonts.get(str(operands[0])) if fonts else None
                cur = f.objgen if f is not None else None
            elif o in ("Tj", "'", '"', "TJ") and cur is not None:
                parts = operands[-1] if o == "TJ" else [operands[-1]]
                for p in parts:
                    if isinstance(p, pikepdf.String):
                        used.setdefault(cur, set()).update(bytes(p))
        for _, x in res.get("/XObject", {}).items():
            if x.get("/Subtype") == Name.Form and x.objgen not in done:
                done.add(x.objgen)
                walk(x, x.get("/Resources"))

    for page in pdf.pages:
        walk(page, page.obj.get("/Resources"))
    return used


def _cmap_glyph_zero(data: bytes, codes: set[int]) -> list[int]:
    """Of `codes`, those a simple TrueType font maps to glyph 0 (.notdef), from its format 0/6 cmap."""
    try:
        num = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(num):
            tag, _, off, ln = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        off = tables[b"cmap"]
        nsub = struct.unpack(">H", data[off + 2: off + 4])[0]
        cmap: dict[int, int] = {}
        for k in range(nsub):
            _, _, so = struct.unpack(">HHI", data[off + 4 + 8 * k: off + 12 + 8 * k])
            fmt = struct.unpack(">H", data[off + so: off + so + 2])[0]
            if fmt == 0:
                cmap.update({c: data[off + so + 6 + c] for c in range(256)})
            elif fmt == 6:
                start, count = struct.unpack(">HH", data[off + so + 6: off + so + 10])
                for j in range(count):
                    cmap[start + j] = struct.unpack(">H", data[off + so + 10 + 2 * j: off + so + 12 + 2 * j])[0]
        return sorted(c for c in codes if cmap.get(c, 0) == 0)
    except (KeyError, struct.error, IndexError):
        return []


def problems(data: bytes) -> list[str]:
    """PDF/A-3b rules Benchtop could break, checked; [] when none is."""
    out: list[str] = []
    if not re.match(rb"%PDF-1\.[0-7]\r?\n%[\x80-\xff]{4}", data[:20]):
        out.append("header: not %PDF-1.n followed by a binary comment")
    pdf = pikepdf.open(io.BytesIO(data))
    root = pdf.Root
    if pdf.is_encrypted or "/Encrypt" in pdf.trailer:
        out.append("encrypted")
    ids = pdf.trailer.get("/ID")
    if ids is None or len(ids) != 2:
        out.append("no document ID")
    # metadata
    md = root.get("/Metadata")
    if md is None:
        out.append("no XMP metadata")
    else:
        if "/Filter" in md:
            out.append("the XMP metadata stream is compressed")
        meta = pdf.open_metadata()
        if meta.get("pdfaid:part") != "3" or meta.get("pdfaid:conformance") != "B":
            out.append(f"XMP says PDF/A part {meta.get('pdfaid:part')} conformance {meta.get('pdfaid:conformance')}")
        info = pdf.docinfo
        pairs = [("/Title", "dc:title"), ("/Producer", "pdf:Producer"), ("/Creator", "xmp:CreatorTool"),
                 ("/Keywords", "pdf:Keywords"), ("/Subject", "dc:description")]
        for k, x in pairs:
            if k in info and str(info[k]) != str(meta.get(x)):
                out.append(f"Info {k} {str(info[k])!r} differs from XMP {x} {meta.get(x)!r}")
        if "/Author" in info and str(info["/Author"]) != "; ".join(meta.get("dc:creator") or []):
            out.append("Info /Author differs from XMP dc:creator")
        for k, x in (("/CreationDate", "xmp:CreateDate"), ("/ModDate", "xmp:ModifyDate")):
            if k in info:
                d = re.sub(r"[^0-9]", "", str(info[k]))[:14]
                xd = re.sub(r"[^0-9]", "", str(meta.get(x) or ""))[:14]
                if d != xd:
                    out.append(f"Info {k} differs from XMP {x}")
        for k in ("/Trapped",):
            if k in info:
                out.append(f"Info {k} without its XMP twin")
    # colour
    intents = [i for i in root.get("/OutputIntents", []) if i.get("/S") == Name.GTS_PDFA1]
    if len(intents) != 1:
        out.append(f"{len(intents)} PDF/A output intents (one is needed)")
    else:
        prof = intents[0].get("/DestOutputProfile")
        icc = prof.read_bytes() if prof is not None else b""
        if prof is None or int(prof.get("/N", 0)) != 3 or icc[12:16] not in (b"mntr", b"prtr") or icc[16:20] != b"RGB " or icc[8] > 4:
            out.append("the output intent's profile isn't an RGB monitor/printer ICC profile of version 4 or older")
    # fonts: every one embedded, and no character drawn with glyph 0
    codes = used_codes(pdf)
    for f in _fonts(pdf):
        sub = f.get("/Subtype")
        if sub == Name.Type3:
            continue
        if sub == Name.Type0:
            continue  # its descendant is checked
        fd = f.get("/FontDescriptor")
        if fd is None or not any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            out.append(f"font {f.get('/BaseFont')} is not embedded")
            continue
        if sub == Name.TrueType and "/FontFile2" in fd and "/FirstChar" in f:
            zero = _cmap_glyph_zero(fd.FontFile2.read_bytes(), codes.get(f.objgen, set()))
            if zero:
                out.append(f"font {f.BaseFont} maps codes {zero[:5]} to .notdef")
        if sub == Name.TrueType and int(fd.get("/Flags", 0)) & 4 and "/Encoding" in f:
            out.append(f"symbolic TrueType font {f.BaseFont} has an /Encoding")
    # no JavaScript, no actions that run things, annotations that print
    names = root.get("/Names", {})
    if "/JavaScript" in names or "/AA" in root or ("/OpenAction" in root and isinstance(root.OpenAction, pikepdf.Dictionary)
                                                     and root.OpenAction.get("/S") == Name.JavaScript):
        out.append("JavaScript or automatic actions")
    for n, page in enumerate(pdf.pages, 1):
        if "/AA" in page.obj:
            out.append(f"page {n} has automatic actions")
        for a in page.obj.get("/Annots", []):
            flags = int(a.get("/F", 0))
            if not flags & 4 or flags & (1 | 2 | 32):
                out.append(f"page {n}: an annotation that doesn't print (F={flags})")
            act = a.get("/A")
            if act is not None and act.get("/S") in (Name.JavaScript, Name.Launch):
                out.append(f"page {n}: an annotation runs {act.S}")
        for _, x in page.obj.get("/Resources", {}).get("/XObject", {}).items():
            if x.get("/Subtype") == Name.Image and ("/Alternates" in x or "/OPI" in x or x.get("/Filter") == Name.LZWDecode):
                out.append(f"page {n}: an image PDF/A doesn't allow")
    # attached files: each an associated file of the document, with its type and relationship
    af = {o.objgen for o in root.get("/AF", [])}
    for name, spec in pdf.attachments.items():
        o = spec.obj
        if o.objgen not in af:
            out.append(f"attachment {name} isn't in the document's /AF")
        if "/AFRelationship" not in o or "/UF" not in o or "/F" not in o:
            out.append(f"attachment {name} lacks /AFRelationship, /UF or /F")
        ef = o.get("/EF", {}).get("/F")
        if ef is None or "/Subtype" not in ef or "/Params" not in ef or "/ModDate" not in ef.Params:
            out.append(f"attachment {name}: its file has no MIME type or modification date")
    return out


def attachments(data: bytes) -> dict[str, bytes]:
    pdf = pikepdf.open(io.BytesIO(data))
    return {name: spec.get_file().read_bytes() for name, spec in pdf.attachments.items()}


def relationships(data: bytes) -> dict[str, str]:
    pdf = pikepdf.open(io.BytesIO(data))
    return {name: str(spec.obj.AFRelationship) for name, spec in pdf.attachments.items()}


def outline(data: bytes) -> list[tuple[str, int]]:
    """The bookmarks: (title, page number from 1)."""
    pdf = pikepdf.open(io.BytesIO(data))
    pages = {p.objgen: i + 1 for i, p in enumerate(pdf.pages)}
    with pdf.open_outline() as ol:
        out = []
        for item in ol.root:
            dest = item.destination
            if isinstance(dest, pikepdf.Array):
                out.append((item.title, pages.get(dest[0].objgen, 0)))
            else:
                out.append((item.title, 0))
    return out


def text(data: bytes, pages: Any = None) -> str:
    """The text of the pages (pypdf, which xhtml2pdf already needs)."""
    import pypdf
    r = pypdf.PdfReader(io.BytesIO(data))
    sel = r.pages if pages is None else [r.pages[i] for i in pages]
    return "\n".join(p.extract_text() or "" for p in sel)


def page_count(data: bytes) -> int:
    return len(pikepdf.open(io.BytesIO(data)).pages)


def images(data: bytes, min_width: int = 0) -> int:
    """How many image XObjects the pages show, at least `min_width` pixels wide."""
    pdf = pikepdf.open(io.BytesIO(data))
    n = 0
    for page in pdf.pages:
        for _, x in page.obj.get("/Resources", {}).get("/XObject", {}).items():
            if x.get("/Subtype") == Name.Image and int(x.get("/Width", 0)) >= min_width:
                n += 1
    return n
