"""Encrypted storage for tokens that change between runs (the Instagram token is refreshed every
few weeks and GitHub Actions cannot rewrite its own secrets). Encrypted with STATE_KEY (a GitHub
secret) and kept in state/vault.bin. The repo is public, so nothing here is ever in plain text.

Make a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json
import os

from pipeline import state


def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("STATE_KEY")
    if not key:
        raise RuntimeError("STATE_KEY secret is not set")
    return Fernet(key.encode())


def load():
    p = state.STATE / "vault.bin"
    if not p.exists():
        return {}
    return json.loads(_fernet().decrypt(p.read_bytes()))


def save(data):
    state.STATE.mkdir(parents=True, exist_ok=True)
    (state.STATE / "vault.bin").write_bytes(_fernet().encrypt(json.dumps(data).encode()))
