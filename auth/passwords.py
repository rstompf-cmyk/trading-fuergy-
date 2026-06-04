# -*- coding: utf-8 -*-
"""auth.passwords — bcrypt heslo hashing.

bcrypt je industry štandard — pomalý hash chránia pred brute force,
zabudovaný salt zabráni rainbow tables.

Použitie:
    h = hash_password("admin")          # → '$2b$12$...'
    verify_password("admin", h)         # → True
    verify_password("wrong", h)         # → False
"""
from __future__ import annotations
import bcrypt


def hash_password(plain: str) -> str:
    """Vráti bcrypt hash hesla. Salt je v hashi (automaticky).

    Bezpečnosť: cost=12 (4096 iterácií) — ~0.3s per hash na bežnom CPU.
    """
    if not plain:
        raise ValueError("Heslo nesmie byť prázdne")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Overí heslo voči bcrypt hashu. False pri nezhode alebo invalid hashi."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
