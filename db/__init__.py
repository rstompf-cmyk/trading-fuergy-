# -*- coding: utf-8 -*-
"""db — SQLAlchemy ORM package pre Fázu 1 migrácie.

Verejné API:
    from db import Session, get_session, engine, Base
    from db.models import Profile, Plan, PlanSlot, User, ...

Cieľ:
    Single source of truth pre všetky perzistentné dáta. Refactor pôvodných
    modulov (profiles.py, plan_store.py, ...) presunie ich internal storage
    z JSON/CSV súborov na DB queries. Verejné API modulov ostáva identické.

Konfigurácia DB URL:
    Env var ``DB_URL`` (napr. ``sqlite:///db/data/app.db`` alebo
    ``postgresql://user:pass@localhost/dbname``). Fallback: SQLite v ``db/data/app.db``.

Vlákna:
    SQLite je single-writer. Pre FastAPI použij ``with get_session() as s:`` pattern
    v každom request handleri — nezdieľaj session medzi requestami.
"""
from .session import Session, get_session, engine, Base, init_db

__all__ = ["Session", "get_session", "engine", "Base", "init_db"]
