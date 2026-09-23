#!/usr/bin/env python3
"""Genera password_hash para config/users.json (mismo algoritmo que app.py)."""
import hashlib
import secrets
import sys


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 hash_password.py 'tu-contraseña'", file=sys.stderr)
        raise SystemExit(1)
    print(hash_password(sys.argv[1]))
