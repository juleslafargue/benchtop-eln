"""Versioned database upgrades.

The database records which upgrade steps it has had (table `benchtop_schema`). On startup Benchtop
creates any new tables, adds any new columns, then runs the numbered steps below that this database
hasn't had yet, in order, each in its own transaction. `./benchtop upgrade` makes a backup first.

Adding a step: append a Migration with the next number. A step must
  * work on a brand-new, empty database too (fresh installs run every step), and
  * never change content that a signature covers (entries, titles, attachments) — signed records
    must still verify after an upgrade. Add columns, indexes and new tables freely; when existing
    data needs reshaping, put the new shape in a new column or table instead.

A database that has had steps this version doesn't know (it was opened by a newer Benchtop) is
refused, so an older version can't misread or damage it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection, Engine, inspect, text

from . import __version__

log = logging.getLogger("benchtop.migrations")
TABLE = "benchtop_schema"


@dataclass(frozen=True)
class Migration:
    number: int
    description: str
    run: Callable[[Connection], None]
    # SQLite: rebuilding a table others point at needs reference checks off while it runs (the step checks them itself)
    foreign_keys_off: bool = False


class TooNew(RuntimeError):
    """The database was upgraded by a newer Benchtop than this one."""


def _baseline(conn: Connection) -> None:
    """Databases from before versioned upgrades: nothing to change, just start counting."""


def _indexes_for_large_labs(conn: Connection) -> None:
    # lists sort by last change and filter by author/status; section scans filter by kind
    for name, table, cols in [
        ("ix_experiment_updated_at", "experiment", "updated_at"),
        ("ix_experiment_owner_status", "experiment", "owner_id, status"),
        ("ix_entry_kind_deleted", "entry", "kind, deleted"),
        ("ix_signature_experiment_meaning", "signature", "experiment_id, meaning"),
    ]:
        conn.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" ({cols})'))


def _backups_keep_files_beside_the_zip(conn: Connection) -> None:
    """Nothing in the database changes. From 0.15 a backup's attached files sit beside the zip, in the shared
    benchtop-files folder; this step raises the version backups record, so an older Benchtop refuses to restore
    one (it would bring back the records without their files) and says to install the newer version first."""


def _locked_status_for_imports(conn: Connection) -> None:
    """Records imported from another ELN, where they were signed or locked, arrive read-only with status "locked".
    SQLite stores the status as text, so nothing changes there; Postgres keeps a list of allowed values. This step also
    raises the version, so an older Benchtop (which doesn't know "locked" and would fail on such a record) refuses
    the data and says to install this version, instead of breaking on the first imported record."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TYPE experimentstatus ADD VALUE IF NOT EXISTS 'locked'"))


def _references_sections(conn: Connection) -> None:
    """A new kind of section, References (papers looked up by DOI). SQLite stores the kind as text, so nothing changes
    there; Postgres keeps a list of allowed values. Like step 4, this raises the version so an older Benchtop (which
    would fail on a References section) refuses the data and says to install this version."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TYPE entrykind ADD VALUE IF NOT EXISTS 'references'"))


def _notebook_numbers_never_reused(conn: Connection) -> None:
    """Until 0.30.0 a deleted notebook's number went to the next notebook made (SQLite numbers a new row one above the
    highest), so a late or repeated request for the deleted one reached the new one, and the audit trail had two
    notebooks under one number. The notebook table is rebuilt as AUTOINCREMENT, which never goes back: the same
    columns, rows and numbers, nothing else touched. Its counter starts above every number a notebook has had, deleted
    ones included (the audit trail lists them). Postgres numbers rows from a sequence, which never goes back: nothing
    to do there. New databases have the table this way already, so only the counter is set."""
    import re
    if conn.dialect.name != "sqlite":
        return
    sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='notebook'")).scalar()
    if sql is None:
        return
    if "AUTOINCREMENT" not in sql.upper():
        if conn.execute(text("PRAGMA foreign_keys")).scalar():
            log.warning("Database upgrade 6: references are being checked, so the notebook table can't be rebuilt; "
                        "deleted notebooks' numbers may still be given again")
            return
        conn.execute(text("UPDATE notebook SET id = id WHERE 0"))  # opens the transaction: the rebuild is all or nothing
        new = re.sub(r'^\s*CREATE\s+TABLE\s+"?notebook"?', "CREATE TABLE notebook_rebuilt", sql, count=1, flags=re.I)
        new = re.sub(r'("?\bid\b"?\s+INTEGER\s+NOT\s+NULL)(\s*,)', r"\1 PRIMARY KEY AUTOINCREMENT\2", new, count=1, flags=re.I)
        new = re.sub(r',\s*PRIMARY\s+KEY\s*\(\s*"?id"?\s*\)', "", new, count=1, flags=re.I)
        conn.execute(text("DROP TABLE IF EXISTS notebook_rebuilt"))
        conn.execute(text(new))
        cols = [tuple(r)[1:] for r in conn.execute(text('PRAGMA table_info("notebook")'))]
        if [tuple(r)[1:] for r in conn.execute(text('PRAGMA table_info("notebook_rebuilt")'))] != cols or \
                "AUTOINCREMENT" not in new.upper():
            raise RuntimeError(f"The notebook table couldn't be rebuilt with the same columns: {sql}")
        others = [r[0] for r in conn.execute(text("SELECT sql FROM sqlite_master WHERE tbl_name='notebook' "
                                                  "AND type IN ('index', 'trigger') AND sql IS NOT NULL"))]
        refs = sorted(tuple(r) for r in conn.execute(text("PRAGMA foreign_key_check")) if r[2] == "notebook")
        names = ", ".join(f'"{c[0]}"' for c in cols)
        n = conn.execute(text("SELECT COUNT(*) FROM notebook")).scalar()
        conn.execute(text(f"INSERT INTO notebook_rebuilt ({names}) SELECT {names} FROM notebook ORDER BY id"))
        conn.execute(text("DROP TABLE notebook"))
        conn.execute(text("ALTER TABLE notebook_rebuilt RENAME TO notebook"))
        for s in others:
            conn.execute(text(s))
        if conn.execute(text("SELECT COUNT(*) FROM notebook")).scalar() != n or \
                sorted(tuple(r) for r in conn.execute(text("PRAGMA foreign_key_check")) if r[2] == "notebook") != refs:
            raise RuntimeError("The rebuilt notebook table doesn't hold the same notebooks")
    highest = conn.execute(text("SELECT MAX(id) FROM notebook")).scalar() or 0
    if conn.execute(text("SELECT 1 FROM sqlite_master WHERE name='auditevent'")).first():
        highest = max(highest, conn.execute(text("SELECT MAX(object_id) FROM auditevent WHERE object_type='notebook'")).scalar() or 0)
    if conn.execute(text("SELECT 1 FROM sqlite_sequence WHERE name='notebook'")).first():
        conn.execute(text("UPDATE sqlite_sequence SET seq = :h WHERE name='notebook' AND seq < :h"), {"h": highest})
    elif highest:
        conn.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('notebook', :h)"), {"h": highest})


MIGRATIONS: list[Migration] = [
    Migration(1, "Start tracking database upgrades", _baseline),
    Migration(2, "Indexes for labs with thousands of experiments", _indexes_for_large_labs),
    Migration(3, "Backups keep attached files once, beside the zips", _backups_keep_files_beside_the_zip),
    Migration(4, "Read-only status for records imported from other ELNs", _locked_status_for_imports),
    Migration(5, "References sections (papers by DOI)", _references_sections),
    Migration(6, "A deleted notebook's number is never given again", _notebook_numbers_never_reused, foreign_keys_off=True),
]
SCHEMA_VERSION = MIGRATIONS[-1].number
assert [m.number for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1)), "migration numbers must be 1, 2, 3…"


def _ensure_table(conn: Connection) -> None:
    conn.execute(text(f'CREATE TABLE IF NOT EXISTS "{TABLE}" (number INTEGER PRIMARY KEY, description TEXT NOT NULL, '
                      'app_version TEXT NOT NULL, applied_at TEXT NOT NULL)'))


def applied(engine: Engine) -> list[dict]:
    if not inspect(engine).has_table(TABLE):
        return []
    with engine.connect() as conn:
        rows = conn.execute(text(f'SELECT number, description, app_version, applied_at FROM "{TABLE}" ORDER BY number')).all()
    return [dict(r._mapping) for r in rows]


def current_version(engine: Engine) -> int:
    done = applied(engine)
    return max((r["number"] for r in done), default=0)


def check_not_too_new(engine: Engine) -> None:
    v = current_version(engine)
    if v > SCHEMA_VERSION:
        newest = max(applied(engine), key=lambda r: r["number"])
        raise TooNew(
            f"This data was last opened by a newer Benchtop ({newest['app_version']}, database version {v}); "
            f"this copy is {__version__} (database version {SCHEMA_VERSION}). Nothing was changed. "
            f"Install Benchtop {newest['app_version']} or newer, or restore a backup made with this version.")


def upgrade(engine: Engine) -> list[Migration]:
    """Run every step this database hasn't had. Returns the steps that ran."""
    check_not_too_new(engine)
    with engine.begin() as conn:
        _ensure_table(conn)
    done = {r["number"] for r in applied(engine)}
    ran = []
    for m in MIGRATIONS:
        if m.number in done:
            continue
        log.info("Database upgrade %d: %s", m.number, m.description)
        with engine.connect() as conn:
            was = None
            if m.foreign_keys_off and conn.dialect.name == "sqlite":  # SQLite takes this only between transactions
                was = conn.execute(text("PRAGMA foreign_keys")).scalar()
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.commit()
            try:
                with conn.begin():  # one transaction per step: a failure leaves earlier steps in place
                    m.run(conn)
                    conn.execute(text(f'INSERT INTO "{TABLE}" (number, description, app_version, applied_at) VALUES (:n, :d, :v, :t)'),
                                 {"n": m.number, "d": m.description, "v": __version__, "t": datetime.now(timezone.utc).isoformat()})
            finally:
                if was is not None:
                    conn.execute(text(f"PRAGMA foreign_keys={'ON' if was else 'OFF'}"))
                    conn.commit()
        ran.append(m)
    return ran
