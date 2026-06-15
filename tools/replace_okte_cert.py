#!/usr/bin/env python3
"""replace_okte_cert.py — výmena OKTE mTLS certifikátu z .p12 → cert.crt + cert.key.

Náhrada za install_okte_cert.sh pre prostredie BEZ openssl CLI (napr. Docker
runtime kontajner python:3.13-slim). Používa python `cryptography` (už je v
requirements), takže funguje aj vo vnútri bežiaceho kontajnera cez `docker exec`.

Výstup ide do out/sk/okte_vdt/ (cert.crt + cert.key, rovnaké názvy ako predtým),
takže okte_vdt_config.json sa NEMUSÍ meniť — len sa prepíšu súbory.

POUŽITIE (vnútri kontajnera, kde je /app/out namountované):
    python tools/replace_okte_cert.py /app/out/sk/okte_vdt/new.p12

  - Heslo k .p12 zadáš interaktívne (neukladá sa), alebo cez env P12_PASSWORD.
  - --out-dir prepíše cieľový priečinok (default out/sk/okte_vdt).
  - --keep-p12 nechá .p12 na disku (default = zmaže po úspechu).

Skript na konci vypíše subject/issuer/platnosť, nech vidíš že je to ten správny
a ešte nevypršaný certifikát.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12


def _default_out_dir() -> str:
    # OKTE VDT je SK-specific → vždy out/sk/okte_vdt (rovnako ako okte_vdt._data_dir)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "out", "sk", "okte_vdt")


def main() -> int:
    ap = argparse.ArgumentParser(description="Výmena OKTE .p12 → cert.crt + cert.key")
    ap.add_argument("p12", help="cesta k novému .p12 súboru")
    ap.add_argument("--out-dir", default=None, help="cieľový priečinok (default out/sk/okte_vdt)")
    ap.add_argument("--keep-p12", action="store_true", help="nezmazať .p12 po úspechu")
    args = ap.parse_args()

    p12_path = args.p12
    if not os.path.isfile(p12_path):
        print(f"✗ Súbor neexistuje: {p12_path}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    crt_path = os.path.join(out_dir, "cert.crt")
    key_path = os.path.join(out_dir, "cert.key")

    pwd = os.environ.get("P12_PASSWORD")
    if pwd is None:
        pwd = getpass.getpass("Heslo k .p12 (Enter ak žiadne): ")
    password = pwd.encode() if pwd else None

    try:
        with open(p12_path, "rb") as f:
            data = f.read()
        key, cert, chain = pkcs12.load_key_and_certificates(data, password)
    except Exception as e:
        print(f"✗ Nepodarilo sa načítať .p12 (zlé heslo alebo poškodený súbor?): {e}", file=sys.stderr)
        return 2

    if cert is None or key is None:
        print("✗ .p12 neobsahuje certifikát aj privátny kľúč.", file=sys.stderr)
        return 2

    # Záloha predchádzajúcich súborov (.bak), keby bolo treba revertnúť
    for p in (crt_path, key_path):
        if os.path.exists(p):
            try:
                os.replace(p, p + ".bak")
            except OSError:
                pass

    # cert.crt = leaf cert + prípadný reťazec (PEM)
    pem_cert = cert.public_bytes(serialization.Encoding.PEM)
    for c in (chain or []):
        pem_cert += c.public_bytes(serialization.Encoding.PEM)
    with open(crt_path, "wb") as f:
        f.write(pem_cert)

    # cert.key = privátny kľúč bez šifrovania (chránime cez permissions 600)
    pem_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(key_path, "wb") as f:
        f.write(pem_key)

    try:
        os.chmod(crt_path, 0o600)
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # Windows host bez POSIX permissions — nevadí

    print(f"✓ Zapísané:\n    {crt_path}\n    {key_path}")
    print("\n▸ Informácie o certifikáte:")
    try:
        print(f"    subject : {cert.subject.rfc4514_string()}")
        print(f"    issuer  : {cert.issuer.rfc4514_string()}")
        # not_valid_*_utc je novšie API; fallback na staršie
        nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        print(f"    platný od : {nb}")
        print(f"    platný do : {na}")
    except Exception as e:
        print(f"    (nepodarilo sa prečítať detaily: {e})")

    if not args.keep_p12:
        try:
            os.remove(p12_path)
            print(f"\n▸ .p12 zmazaný z disku: {p12_path}")
        except OSError as e:
            print(f"\n⚠ .p12 sa nepodarilo zmazať ({e}) — zmaž ho ručne.")

    print("\nHotovo. Reštartuj kontajner a over cez OKTE probe v appke.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
