"""Per-tenant Google token custody (ADR 0007).

Each connected guest's OAuth token is stored keyed by their principal id, ENCRYPTED at rest
with Fernet (key from ARIA_TOKEN_ENC_KEY). A disk read alone doesn't yield a usable token.
The owner does NOT use this — the owner keeps token.json (ADR 0003); this is guests only.

Layout: tokens/<safe_id>.gtok, each holding Fernet(ciphertext of the token JSON).
Gitignored. Losing the key just means guests re-connect; it never exposes owner mail.
"""
import os
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from tenant import safe_id

TOKENS_DIR = Path(os.path.join(os.path.dirname(__file__), "tokens"))


def configured() -> bool:
    """True only when an encryption key is set — guest token storage is off without it."""
    return bool(os.getenv("ARIA_TOKEN_ENC_KEY"))


def _fernet() -> Fernet:
    key = os.getenv("ARIA_TOKEN_ENC_KEY")
    if not key:
        raise RuntimeError("ARIA_TOKEN_ENC_KEY not set — refusing to handle guest tokens.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def generate_key() -> str:
    """A fresh Fernet key (urlsafe base64) for ARIA_TOKEN_ENC_KEY. Run once, store in .env."""
    return Fernet.generate_key().decode()


def _path(user_id: str) -> Path:
    return TOKENS_DIR / f"{safe_id(user_id)}.gtok"


def save(user_id: str, token_data: dict):
    """Encrypt and persist a guest's token JSON, keyed by their principal id."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    blob = _fernet().encrypt(json.dumps(token_data).encode())
    p = _path(user_id)
    p.write_bytes(blob)
    os.chmod(p, 0o600)


def load(user_id: str):
    """Decrypt and return a guest's token JSON, or None if not connected / unreadable."""
    p = _path(user_id)
    if not p.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(p.read_bytes()).decode())
    except (InvalidToken, ValueError, OSError):
        return None      # corrupt / wrong key / unreadable — treat as not connected


def has(user_id: str) -> bool:
    return _path(user_id).exists()


def delete(user_id: str) -> bool:
    """Remove a guest's stored token (disconnect / account removal). True if one was there."""
    p = _path(user_id)
    if p.exists():
        p.unlink()
        return True
    return False
