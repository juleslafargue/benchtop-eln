"""Plugin files: new section types and instrument-file readers, described in TOML (no code).

A plugin is one .toml file. Benchtop looks in two places:
  * plugins/ in the Benchtop folder: examples that ship with Benchtop (off until an admin turns them on)
  * <data folder>/plugins/: your lab's own plugins (added from Admin › Plugins, or copied there)

Sections created from a plugin store a copy of their own definition, so a record stays readable,
verifiable and printable even if the plugin is later changed or removed.
See docs/PLUGINS.md for the format.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

API_VERSION = 1
BUNDLED_DIR = Path(os.environ.get("BENCHTOP_BUNDLED_PLUGINS_DIR") or Path(__file__).resolve().parents[2] / "plugins")
FIELD_TYPES = {"text", "longtext", "number", "select", "checkbox", "date", "datetime"}
COLUMN_TYPES = {"text", "number", "select", "checkbox", "date"}
READER_KINDS = {"uv", "ir", "ms", "other"}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def user_dir() -> Path:
    d = settings.data_dir / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Plugin:
    id: str
    name: str
    version: str = ""
    author: str = ""
    description: str = ""
    homepage: str = ""
    path: str = ""
    bundled: bool = False
    sections: list[dict] = field(default_factory=list)
    readers: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    enabled: bool = False

    def out(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "version": self.version, "author": self.author, "description": self.description,
                "homepage": self.homepage, "file": Path(self.path).name, "bundled": self.bundled, "enabled": self.enabled,
                "errors": self.errors, "sections": [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in self.sections],
                "readers": [{"id": r["id"], "name": r["name"], "technique": r["technique"]} for r in self.readers]}


class PluginError(ValueError):
    pass


def _s(v: Any, n: int = 200) -> str:
    return str(v if v is not None else "").strip()[:n]


def _num_or_none(v: Any) -> float | None:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


MAX_ROWS = 500      # rows a table keeps (clean_content); min_rows can't ask for more
MAX_OPTIONS = 60    # choices in a select
MAX_DECIMALS = 10


# 0.36.6: each value is checked for its type, so a mistake gets a message naming it (lists where a table was meant, a
# word where a number was, "no" for false…) instead of an Internal Server Error, or a limit that made no sense (min_rows
# of -3, 2.5 or 100 000 000; decimals of 500). Definitions stored with sections made before then are only tidied
# (strict=False): those records must still open and save as they did.

def _what(v: Any) -> str:
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        return "a table"
    if isinstance(v, list):
        return "a list"
    return type(v).__name__


def _list(v: Any, where: str, of: str) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        raise PluginError(f"{where} must be {of}, not {_what(v)}")
    return v


def _bool(v: Any, where: str, strict: bool) -> bool:
    if v is None or isinstance(v, bool) or not strict:
        return bool(v)
    raise PluginError(f"{where} must be true or false (without quotes), not {_what(v)}")


def _number(v: Any, where: str, strict: bool) -> float | None:
    if v is None or not strict:
        return _num_or_none(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or _num_or_none(v) is None:
        raise PluginError(f"{where} must be a number (without quotes), not {_what(v)}")
    return float(v)


def _whole(v: Any, where: str, lo: int, hi: int, default: int, strict: bool) -> int:
    if not strict:  # tidy a stored definition: anything odd becomes the nearest sensible value
        f = _num_or_none(v)
        return default if f is None else max(lo, min(hi, int(f)))
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise PluginError(f"{where} must be a whole number from {lo} to {hi}, not {v!r}")
    if not lo <= v <= hi:
        raise PluginError(f"{where} must be from {lo} to {hi}, not {int(v)}")
    return int(v)


def _field(f: dict, where: str, types: set[str], strict: bool = True) -> dict:
    if not isinstance(f, dict):
        raise PluginError(f"{where}: each field must be a table")
    if strict and not isinstance(f.get("id"), str):
        raise PluginError(f"{where}: each field needs an id (text, in quotes)")
    fid = _s(f.get("id"), 48)
    if not re.match(r"^[a-z0-9_][a-z0-9_-]*$", fid):
        raise PluginError(f"{where}: field id “{fid}” must use lowercase letters, digits, _ or -")
    t = _s(f.get("type") or "text", 20)
    if t not in types:
        raise PluginError(f"{where}.{fid}: type “{t}” isn't one of {', '.join(sorted(types))}")
    out: dict[str, Any] = {"id": fid, "label": _s(f.get("label") or fid, 80), "type": t}
    for k in ("unit", "help", "placeholder"):
        if f.get(k):
            out[k] = _s(f[k], 200)
    if _bool(f.get("required"), f"{where}.{fid}: required", strict):
        out["required"] = True
    if t == "select":
        opts = f.get("options")
        if not isinstance(opts, list) or not opts:
            raise PluginError(f"{where}.{fid}: a select needs a list of options")
        if strict:
            if any(isinstance(o, (dict, list, bool)) for o in opts):
                raise PluginError(f"{where}.{fid}: each option must be a word or a number")
            if len(opts) > MAX_OPTIONS:
                raise PluginError(f"{where}.{fid}: a select can have up to {MAX_OPTIONS} options, not {len(opts)}")
        out["options"] = [_s(o, 80) for o in opts][:MAX_OPTIONS]
    if t == "number":
        for k in ("min", "max", "warn_below", "warn_above"):
            v = _number(f.get(k), f"{where}.{fid}: {k}", strict)
            if v is not None:
                out[k] = v
        if f.get("decimals") is not None:
            out["decimals"] = _whole(f.get("decimals"), f"{where}.{fid}: decimals", 0, MAX_DECIMALS, 6, strict)
        if strict and "min" in out and "max" in out and out["min"] > out["max"]:
            raise PluginError(f"{where}.{fid}: min ({out['min']:g}) is larger than max ({out['max']:g})")
    if "default" in f and t != "longtext":
        d = f["default"]
        if strict and t == "checkbox" and not isinstance(d, bool):
            raise PluginError(f"{where}.{fid}: the default of a checkbox must be true or false (without quotes)")
        if strict and t == "number":
            d = _number(d, f"{where}.{fid}: default", strict)
        if strict and t == "select" and _s(d, 80) not in out["options"]:
            raise PluginError(f"{where}.{fid}: the default “{_s(d, 80)}” isn't one of the options")
        if strict and t not in ("checkbox", "number") and isinstance(d, (dict, list)):
            raise PluginError(f"{where}.{fid}: default must be text, not {_what(d)}")
        out["default"] = d if t in ("checkbox", "number") else _s(d, 200)
    if f.get("width") in ("half", "full", "third"):
        out["width"] = f["width"]
    return out


def _section(sec: dict, pid: str, strict: bool = True) -> dict:
    if not isinstance(sec, dict):
        raise PluginError(f"each section must be a table, written [[sections]], not {_what(sec)}")
    sid = _s(sec.get("id"), 48)
    if not ID.match(sid) or (strict and not isinstance(sec.get("id"), str)):
        raise PluginError(f"section id “{sid}” must be lowercase letters, digits and dashes")
    where = f"sections.{sid}"
    fields = [_field(f, where, FIELD_TYPES, strict) for f in _list(sec.get("fields"), f"{where}.fields",
                                                                    "a list of tables, written [[sections.fields]]")]
    tables = []
    for t in _list(sec.get("tables"), f"{where}.tables", "a list of tables, written [[sections.tables]]"):
        if not isinstance(t, dict):
            raise PluginError(f"{where}: each table must be a table, written [[sections.tables]], not {_what(t)}")
        tid = _s(t.get("id"), 48)
        if not re.match(r"^[a-z0-9_][a-z0-9_-]*$", tid):
            raise PluginError(f"{where}: table id “{tid}” must use lowercase letters, digits, _ or -")
        cols = [_field(c, f"{where}.{tid}", COLUMN_TYPES, strict) for c in _list(
            t.get("columns"), f"{where}.{tid}.columns", "a list of tables, written [[sections.tables.columns]]")]
        if not cols:
            raise PluginError(f"{where}.{tid}: a table needs at least one column")
        tables.append({"id": tid, "label": _s(t.get("label") or tid, 80), "columns": cols,
                       "min_rows": _whole(t.get("min_rows"), f"{where}.{tid}: min_rows", 0, MAX_ROWS, 0, strict),
                       "help": _s(t.get("help"), 200)})
    if not fields and not tables:
        raise PluginError(f"{where}: add at least one field or table")
    ids = [f["id"] for f in fields] + [t["id"] for t in tables]
    if len(ids) != len(set(ids)):
        raise PluginError(f"{where}: field and table ids must be unique")
    return {"plugin": pid, "id": sid, "name": _s(sec.get("name") or sid, 80), "title": _s(sec.get("title") or sec.get("name") or sid, 120),
            "description": _s(sec.get("description"), 300), "fields": fields, "tables": tables}


def _reader(r: dict, pid: str) -> dict:
    if not isinstance(r, dict):
        raise PluginError(f"each reader must be a table, written [[readers]], not {_what(r)}")
    rid = _s(r.get("id"), 48)
    if not ID.match(rid) or not isinstance(r.get("id"), str):
        raise PluginError(f"reader id “{rid}” must be lowercase letters, digits and dashes")
    for k in ("detect", "filename", "data_after", "name", "technique", "conditions", "x_label", "y_label", "kind", "decimal"):
        if isinstance(r.get(k), (dict, list, bool)):
            raise PluginError(f"readers.{rid}: {k} must be text, not {_what(r[k])}")
    detect = _s(r.get("detect"), 300)
    fname = _s(r.get("filename"), 300)
    try:
        if detect:
            re.compile(detect)
        if fname:
            re.compile(fname)
        if r.get("data_after"):
            re.compile(str(r["data_after"]))
    except re.error as e:
        raise PluginError(f"readers.{rid}: bad pattern ({e})") from None
    raw_exts = r.get("extensions") if r.get("extensions") is not None else [".csv", ".txt"]
    if isinstance(raw_exts, str):
        raw_exts = [raw_exts]  # extensions = ".csv": one is clear enough
    if not isinstance(raw_exts, list) or any(not isinstance(e, str) or not e.strip(".") for e in raw_exts):
        raise PluginError(f"readers.{rid}: extensions must be a list of file endings, like [\".csv\", \".txt\"]")
    exts = [e.lower() if str(e).startswith(".") else "." + str(e).lower() for e in raw_exts][:10]
    kind = _s(r.get("kind") or "other", 10)
    if kind not in READER_KINDS:
        raise PluginError(f"readers.{rid}: kind must be one of {', '.join(sorted(READER_KINDS))}")
    delim = r.get("delimiter", "auto")
    if delim not in ("auto", ",", ";", "\t", "tab", "whitespace", "|"):
        raise PluginError(f"readers.{rid}: delimiter must be auto, \",\", \";\", \"tab\", \"|\" or \"whitespace\"")

    def col(v, name):
        if isinstance(v, bool) or v is None or isinstance(v, (float, list, dict)):
            raise PluginError(f"readers.{rid}: {name} must be a column number (starting at 1) or a header name")
        if isinstance(v, int):
            if v < 1:
                raise PluginError(f"readers.{rid}: column numbers start at 1")
            return v
        return _s(v, 80)

    def scale(name):
        v = _number(r.get(name), f"readers.{rid}: {name}", True)
        if v == 0:
            raise PluginError(f"readers.{rid}: {name} can't be 0 (every value would become 0)")
        return v or 1.0
    return {"plugin": pid, "id": rid, "name": _s(r.get("name") or rid, 80), "technique": _s(r.get("technique") or "Chromatogram", 40),
            "conditions": _s(r.get("conditions"), 120), "kind": kind, "extensions": exts, "detect": detect, "filename": fname,
            "delimiter": "\t" if delim == "tab" else delim, "decimal": "," if r.get("decimal") == "," else ".",
            "data_after": _s(r.get("data_after"), 300), "header": _bool(r.get("header", False), f"readers.{rid}: header", True),
            "x": col(r.get("x", 1), "x"), "y": col(r.get("y", 2), "y"),
            "x_label": _s(r.get("x_label") or "x", 60), "y_label": _s(r.get("y_label") or "Intensity", 60),
            "x_scale": scale("x_scale"), "y_scale": scale("y_scale"),
            "reversed": _bool(r.get("reversed", False), f"readers.{rid}: reversed", True),
            "dips": _bool(r.get("dips", False), f"readers.{rid}: dips", True)}


def parse(text: str, path: str = "", bundled: bool = False) -> Plugin:
    """Read and check one plugin file. Raises PluginError with a plain-English reason."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PluginError(f"Not valid TOML: {e}") from None
    meta = data.get("plugin")
    if not isinstance(meta, dict):
        raise PluginError("Missing the [plugin] table at the top")
    if not isinstance(meta.get("id"), str) or not ID.match(meta["id"].strip()[:48]):
        raise PluginError("plugin id must be lowercase letters, digits and dashes, in quotes, like id = “glovebox-log”")
    pid = _s(meta.get("id"), 48)
    api = meta.get("api", 1)
    if api != API_VERSION or isinstance(api, bool):
        if not isinstance(api, int) or isinstance(api, bool):
            raise PluginError(f"api must be a whole number without quotes (api = {API_VERSION}), not {api!r}")
        raise PluginError(f"Written for plugin API {api}; this Benchtop understands API {API_VERSION}")
    for k in ("name", "version", "author", "description", "homepage"):
        if isinstance(meta.get(k), (dict, list)):
            raise PluginError(f"plugin {k} must be text, not {_what(meta[k])}")
    p = Plugin(id=pid, name=_s(meta.get("name") or pid, 80), version=_s(meta.get("version"), 20), author=_s(meta.get("author"), 80),
               description=_s(meta.get("description"), 400), homepage=_s(meta.get("homepage"), 200), path=path, bundled=bundled)
    p.sections = [_section(s, pid) for s in _list(data.get("sections"), "sections", "a list of tables, written [[sections]]")]
    p.readers = [_reader(r, pid) for r in _list(data.get("readers"), "readers", "a list of tables, written [[readers]]")]
    for r in p.readers:
        if not r["detect"] and not r["filename"]:
            raise PluginError(f"readers.{r['id']}: add a detect pattern (text inside the file) or a filename pattern, so it isn't used for every file")
    if not p.sections and not p.readers:
        raise PluginError("A plugin needs at least one [[sections]] or [[readers]]")
    for kind, items in (("section", p.sections), ("reader", p.readers)):
        ids = [i["id"] for i in items]
        if len(ids) != len(set(ids)):
            raise PluginError(f"Two {kind}s share an id")
    return p


def discover() -> list[Plugin]:
    """Every plugin file, with errors attached rather than raised (so one bad file can't hide the rest)."""
    found: dict[str, Plugin] = {}
    for d, bundled in ((BUNDLED_DIR, True), (user_dir(), False)):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.toml")):
            try:
                p = parse(f.read_text(encoding="utf-8"), str(f), bundled)
            except (PluginError, OSError, UnicodeDecodeError) as e:
                p = Plugin(id=f.stem, name=f.stem, path=str(f), bundled=bundled, errors=[str(e)])
            except Exception as e:  # a mistake the checks above didn't foresee: this file is listed as broken, not the page
                p = Plugin(id=f.stem, name=f.stem, path=str(f), bundled=bundled,
                           errors=[f"Benchtop couldn't read this file ({type(e).__name__}: {e})"[:300]])
            if p.id in found and not found[p.id].errors:
                if bundled is False:  # your own copy wins over a bundled example of the same id
                    found[p.id] = p
                continue
            found[p.id] = p
    return sorted(found.values(), key=lambda p: (p.bundled, p.name.lower()))


def with_state(session) -> list[Plugin]:
    from sqlmodel import select

    from .models import PluginState
    state = {s.plugin_id: s.enabled for s in session.exec(select(PluginState)).all()}
    ps = discover()
    for p in ps:
        # your own plugins are on unless switched off; bundled examples are off unless switched on
        p.enabled = not p.errors and state.get(p.id, not p.bundled or settings.demo)
    return ps


def enabled_sections(session) -> list[dict]:
    return [s for p in with_state(session) if p.enabled for s in p.sections]


def enabled_readers(session) -> list[dict]:
    return [r for p in with_state(session) if p.enabled for r in p.readers]


# ---------------------------------------------------------------- section content

def new_content(section: dict) -> dict:
    values = {f["id"]: f.get("default", False if f["type"] == "checkbox" else None) for f in section["fields"]}
    rows = {t["id"]: [{c["id"]: (False if c["type"] == "checkbox" else None) for c in t["columns"]} for _ in range(max(1, t["min_rows"]))]
            for t in section["tables"]}
    return {"schema": section, "values": values, "rows": rows}


def _value(f: dict, v: Any) -> Any:
    t = f["type"]
    if t == "checkbox":
        return bool(v)
    if v is None or v == "":
        return None
    if t == "number":
        return _num_or_none(v)
    if t == "select":
        return _s(v, 80) if _s(v, 80) in f.get("options", []) else None
    if t in ("date", "datetime"):
        s = _s(v, 25)
        return s if re.match(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$", s) else None
    # text as typed, spaces and new lines included, as in every other section (0.36.4: they were stripped on each
    # save, and the page took the stripped copy back while the person was still typing: "DMEM " + "high glucose"
    # became "DMEMhigh glucose")
    return str(v)[:20000 if t == "longtext" else 500]


def installed_section(session, schema: Any) -> dict | None:
    """The section type, as the installed and switched-on plugin defines it, that a definition sent with a new section
    names (by plugin and section id); None if there's no such plugin or section here."""
    if not isinstance(schema, dict):
        return None
    pid, sid = schema.get("plugin"), schema.get("id")
    return next((s for s in enabled_sections(session) if s["plugin"] == pid and s["id"] == sid), None)


def clean_content(content: dict) -> dict:
    """Keep only what the section's own (stored) definition allows, typed properly. Requests from the app or a script
    never choose that definition (0.36.4): routers/experiments.py puts in the installed plugin's for a new section,
    and the one stored with the section for an edit, before this is called."""
    schema = content.get("schema")
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
        raise PluginError("This section has no definition")
    try:  # re-check the stored definition, so a hand-edited one can't smuggle anything in (tidied, not refused: 0.36.6)
        schema = _section(schema, _s(schema.get("plugin"), 48), strict=False)
    except (PluginError, AttributeError, TypeError) as e:
        raise PluginError(f"This section's definition isn't valid: {e}") from None
    vals = content.get("values") if isinstance(content.get("values"), dict) else {}
    out_vals = {f["id"]: _value(f, vals.get(f["id"])) for f in schema["fields"]}
    rows_in = content.get("rows") if isinstance(content.get("rows"), dict) else {}
    out_rows = {}
    for t in schema["tables"]:
        rows = rows_in.get(t["id"]) if isinstance(rows_in.get(t["id"]), list) else []
        out_rows[t["id"]] = [{c["id"]: _value(c, r.get(c["id"])) for c in t["columns"]} for r in rows[:500] if isinstance(r, dict)]
    return {"schema": schema, "values": out_vals, "rows": out_rows}


def is_blank(v: Any) -> bool:
    """Nothing typed or ticked. Not 0: a passage number or a vehicle's concentration of 0 is a value (before 0.36,
    `v in (None, "", False)` counted 0 as empty, since 0 == False in Python, and such a record couldn't be signed).
    Spaces alone are nothing too: text is kept as typed (0.36.4), so "   " is stored, but doesn't fill a required item."""
    return v is None or v is False or (isinstance(v, str) and v.strip() == "")


def missing_required(content: dict) -> list[str]:
    """Labels of required fields still empty (an unticked required checkbox counts as empty). The sign dialog shows the
    same list before the password is asked for: src/signCheck.ts mirrors this, rule for rule."""
    schema = content.get("schema") or {}
    vals = content.get("values") or {}
    miss = [f["label"] for f in schema.get("fields", []) if f.get("required") and is_blank(vals.get(f["id"]))]
    for t in schema.get("tables", []):
        rows = [r for r in (content.get("rows") or {}).get(t["id"], []) if any(not is_blank(v) for v in r.values())]
        if t.get("min_rows") and len(rows) < t["min_rows"]:
            miss.append(f"{t['label']} (at least {t['min_rows']} row{'s' if t['min_rows'] > 1 else ''})")
        for c in t["columns"]:
            if c.get("required") and any(is_blank(r.get(c["id"])) for r in rows):
                miss.append(f"{t['label']}: {c['label']}")
    return miss


def text_of(content: dict) -> str:
    """Plain text for search and Markdown export (0.36: a value of 0 is written, not left out)."""
    schema = content.get("schema") or {}
    vals = content.get("values") or {}
    parts = []
    for f in schema.get("fields", []):
        v = vals.get(f["id"])
        if not is_blank(v):
            v = v.strip() if isinstance(v, str) else v
            parts.append(f"{f['label']}: {'yes' if v is True else v}{(' ' + f['unit']) if f.get('unit') and f['type'] == 'number' else ''}")
    for t in schema.get("tables", []):
        for r in (content.get("rows") or {}).get(t["id"], []):
            cells = [f"{c['label']} {str(r.get(c['id'])).strip() if isinstance(r.get(c['id']), str) else r.get(c['id'])}"
                     for c in t["columns"] if not is_blank(r.get(c["id"]))]
            if cells:
                parts.append(f"{t['label']}: " + ", ".join(cells))
    return "\n".join(parts)
