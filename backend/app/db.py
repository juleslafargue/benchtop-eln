from __future__ import annotations

import logging

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

log = logging.getLogger("benchtop")

_connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
# FastAPI runs each request on a thread pool (40 threads); with SQLite's default 5+10 connections, busy
# moments queued requests for up to 30 s. SQLite connections are cheap, so allow plenty.
_pool = {"pool_size": 20, "max_overflow": 40, "pool_timeout": 60} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, **_pool)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


class MovedFromPostgres(RuntimeError):
    """This server kept its records in Postgres; starting on an empty SQLite database would hide them."""


def check_not_moved_from_postgres() -> None:
    if not settings.legacy_postgres or not settings.database_url.startswith("sqlite:///"):
        return
    if Path(settings.database_url.removeprefix("sqlite:///")).exists():
        return  # already running on SQLite (moved over on purpose): nothing to protect
    raise MovedFromPostgres(
        "This server kept its records in Postgres, but it has just been started with Benchtop's standard database, "
        "which is empty here. Nothing was changed, and your records are still in Postgres.\n"
        "  To keep using Postgres: start it with deploy/docker-compose.server-postgres.yml instead, and set up its "
        "backups (docs/SERVER.md, \"Postgres (advanced)\").\n"
        "  To start afresh on the standard database instead: remove the POSTGRES_PASSWORD line from deploy/.env.")


class BackupBeforeUpgradeFailed(RuntimeError):
    """The database needs upgrading, and the backup that comes first didn't work."""


def backup_before_upgrade() -> dict | None:
    """Back up when this version is about to upgrade the database. `./benchtop update` backs up first, but code that
    arrived another way (merged in GitHub Desktop, git pull, a copied folder) would otherwise upgrade with no backup
    from just before. The backup keeps the old database version, so the previous Benchtop can restore it."""
    from sqlalchemy import inspect

    from . import backup, migrations

    if not settings.database_url.startswith("sqlite:///") or settings.backup_interval_hours <= 0:
        return None  # Postgres is backed up by its admin; 0 turns automatic backups off (demo, tests)
    db = Path(settings.database_url.removeprefix("sqlite:///"))
    if not db.exists() or not inspect(engine).has_table("experiment"):
        return None  # a new, empty database: nothing to keep
    have = migrations.current_version(engine)
    if have >= migrations.SCHEMA_VERSION:
        return None
    newest = backup.newest_backup_info()
    if newest and newest["schema_version"] == have and newest["age_minutes"] < 10:
        return None  # ./benchtop update made one a moment ago
    try:
        made = backup.create_backup("before-upgrade")
    except Exception as exc:  # noqa: BLE001 - whatever it was, don't upgrade without the backup
        raise BackupBeforeUpgradeFailed(
            f"This version of Benchtop upgrades the database (to version {migrations.SCHEMA_VERSION}), and backs up "
            f"your data first, but the backup didn't work: {exc}\n  Nothing was changed. Fix that (for example, free "
            "some disk space), then start Benchtop again.") from exc
    log.warning("Backed up your data before upgrading the database (version %s to %s): %s",
                have, migrations.SCHEMA_VERSION, made["path"])
    return made


def finish_interrupted_restore() -> None:
    from . import backup

    if settings.database_url.startswith("sqlite:///") and (aside := backup.finish_interrupted_restore()):
        log.warning("Finished restoring a backup that was cut off part-way. The data from before it is in %s", aside)


def init_db() -> None:
    from . import migrations, models  # noqa: F401  (register tables)

    check_not_moved_from_postgres()  # before anything creates an empty database file
    finish_interrupted_restore()  # a restore cut off part-way: finish it before anything opens the database
    migrations.check_not_too_new(engine)  # before touching anything: an older Benchtop mustn't alter newer data
    backup_before_upgrade()  # before the tables, columns and steps below change anything
    SQLModel.metadata.create_all(engine)
    add_missing_columns()
    migrations.upgrade(engine)


def add_missing_columns(eng=None) -> list[str]:
    """Minimal forward migration: add columns that newer versions define but an older
    database lacks. New columns are always nullable, so this is safe on existing rows."""
    from sqlalchemy import inspect, text

    eng = eng or engine
    added = []
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                coltype = col.type.compile(dialect=eng.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                added.append(f"{table.name}.{col.name}")
    return added


def get_session() -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session
