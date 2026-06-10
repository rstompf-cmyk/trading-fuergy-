#!/usr/bin/env python3
"""Fix corrupted alembic_version row in app.db.

Použitie:
    python3 -m tools.fix_alembic_version           # set to last known (a1b2c3d4e5f6)
    python3 -m tools.fix_alembic_version REVISION  # set to specific revision
"""
import sys
import sqlite3
import os

DB = os.environ.get("APP_DB_PATH", "/app/db/data/app.db")
target = sys.argv[1] if len(sys.argv) > 1 else "a1b2c3d4e5f6"

c = sqlite3.connect(DB)
before = c.execute("SELECT version_num FROM alembic_version").fetchone()
print(f"Before: {before}")
c.execute("UPDATE alembic_version SET version_num = ?", (target,))
c.commit()
after = c.execute("SELECT version_num FROM alembic_version").fetchone()
print(f"After:  {after}")
c.close()
print(f"OK — alembic_version set to {target}")
