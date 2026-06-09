# -*- coding: utf-8 -*-
"""db.session — SQLAlchemy engine + session factory.

DB URL z env var ``DB_URL`` (default: SQLite ``db/data/app.db``).
Pre Postgres prepnutie stačí zmeniť env var, žiaden refactor.

Použitie:

    from db import get_session, Profile

    with get_session() as s:
        profile = s.query(Profile).filter_by(name="Trakany").one()
        profile.note = "updated"
        # commit auto cez context manager
"""
from __future__ import annotations
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session as _SASession

# ── DB URL z env (lokálne SQLite, na Postgres stačí zmeniť env var) ─────────
_DEFAULT_URL = "sqlite:///db/data/app.db"
DB_URL = os.environ.get("DB_URL", _DEFAULT_URL)

# SQLite engine flags:
#   • check_same_thread=False — FastAPI async potrebuje zdieľanie cez vlákna
#   • pool_pre_ping — overuje connection pred použitím (auto-reconnect)
_engine_kwargs = {"pool_pre_ping": True}
if DB_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DB_URL, **_engine_kwargs)


# Bug #640 (2026-06-09): zapnúť PRAGMA foreign_keys=ON v každom SQLite connection.
# SQLite default = vypnuté, takže ON DELETE CASCADE nepôsobí. Bez tohto sa
# delete_profile zachytí na IntegrityError lebo Plan má FK profile_id (no cascade).
if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

# Session factory — autoflush=False aby sme mali plnú kontrolu nad commit-mi
SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                                expire_on_commit=False)


class Base(DeclarativeBase):
    """SQLAlchemy 2.x declarative base — všetky ORM modely dedia z neho."""
    pass


@contextmanager
def get_session() -> Iterator[_SASession]:
    """Context manager pre session. Commit pri úspechu, rollback pri chybe.

    Použitie:

        with get_session() as s:
            s.add(obj)
            # auto commit + close

    Pri exception → automatický rollback + raise.
    """
    s = SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# Alias pre back-compat — `Session` ako synonym pre `get_session`
Session = get_session


def init_db(create_tables: bool = True) -> None:
    """Inicializuje DB — vytvorí všetky tabuľky z ORM modelov ak ešte neexistujú.

    V produkcii sa použije alembic migrácie; toto je iba pre rýchly bootstrap
    pri dev/test prostredí alebo prvom spustení.

    Args:
        create_tables: True (default) → CREATE TABLE pre všetky modely;
                       False → iba importnúť modely (registrovať mapping).
    """
    # Import zaregistruje všetky modely v Base.metadata
    from . import models   # noqa: F401
    if create_tables:
        # Zabezpeč že priečinok pre SQLite existuje
        if DB_URL.startswith("sqlite:///"):
            db_path = DB_URL.removeprefix("sqlite:///")
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        Base.metadata.create_all(bind=engine)


__all__ = ["engine", "Base", "Session", "get_session", "init_db", "DB_URL"]
