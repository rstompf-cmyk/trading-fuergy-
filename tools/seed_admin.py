#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seed_admin.py — vytvor admin user-a alebo zmeň heslo existujúceho.

Použitie:
    # interaktívne (heslo si vypýta)
    python tools/seed_admin.py

    # cez argumenty (heslo v plaintexte — iba pre dev)
    python tools/seed_admin.py --username admin --password admin123 --role admin

    # zmeniť heslo existujúceho usera
    python tools/seed_admin.py --username admin --password newpass --update

Predtým musí byť spustený `alembic upgrade head`.
"""
from __future__ import annotations
import argparse
import getpass
import os
import sys
from datetime import datetime

# Pridaj root projektu na path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_session
from db.models import User
from auth.passwords import hash_password
from auth.permissions import VALID_ROLES


def main():
    parser = argparse.ArgumentParser(description="Seed admin/user do DB")
    parser.add_argument("--username", default="admin",
                          help="Username (default: admin)")
    parser.add_argument("--password", default=None,
                          help="Plaintext heslo (ak vynechané, opýta sa interaktívne)")
    parser.add_argument("--role", default="admin", choices=list(VALID_ROLES),
                          help="Role (default: admin)")
    parser.add_argument("--email", default=None, help="E-mail (optional)")
    parser.add_argument("--update", action="store_true",
                          help="Update existujúceho user-a (zmena hesla/role)")
    args = parser.parse_args()

    # Heslo
    if args.password:
        password = args.password
    else:
        password = getpass.getpass(f"Heslo pre '{args.username}': ")
        confirm = getpass.getpass("Heslo znova: ")
        if password != confirm:
            print("❌ Heslá sa nezhodujú.", file=sys.stderr)
            sys.exit(1)
    if len(password) < 4:
        print("❌ Heslo musí mať aspoň 4 znaky.", file=sys.stderr)
        sys.exit(1)

    pwd_hash = hash_password(password)
    now_iso = datetime.now().isoformat(timespec="seconds")

    with get_session() as s:
        existing = s.query(User).filter_by(username=args.username).one_or_none()
        if existing:
            if not args.update:
                print(f"⚠  User '{args.username}' už existuje. Použi --update pre zmenu.",
                      file=sys.stderr)
                sys.exit(2)
            existing.password_hash = pwd_hash
            existing.role = args.role
            if args.email:
                existing.email = args.email
            print(f"✅ User '{args.username}' aktualizovaný (role={args.role}).")
        else:
            user = User(
                username=args.username, role=args.role,
                email=args.email,
                password_hash=pwd_hash,
                is_active=True,
                created_at=now_iso,
            )
            s.add(user)
            s.flush()
            print(f"✅ User '{args.username}' vytvorený (id={user.id}, role={args.role}).")


if __name__ == "__main__":
    main()
