"""Passwords of admin panel accounts: scrypt from the standard library with a random salt per password.

The stored value carries its own parameters («scrypt$n$r$p$salt$key»), so the cost can be raised later
without breaking existing passwords.
"""

import base64
import functools
import hashlib
import hmac
import secrets

SCHEME = "scrypt"
# About 16 MB of memory and a few tens of milliseconds per check: unnoticeable at login, slow for brute force
_N, _R, _P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MAX_MEMORY = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _derive(password, salt, _N, _R, _P)
    return "$".join((SCHEME, str(_N), str(_R), str(_P), _b64(salt), _b64(key)))


def verify_password(password: str, stored: str) -> bool:
    """True if the password matches; False for a wrong password and for a damaged or unknown stored value."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = base64.b64decode(key, validate=True)
        actual = _derive(password, base64.b64decode(salt, validate=True), int(n), int(r), int(p))
    except ValueError:  # also binascii.Error and scrypt's own errors on bad parameters
        return False
    return hmac.compare_digest(actual, expected)


@functools.cache
def dummy_hash() -> str:
    """A hash of a random password: checked when the login is unknown, so a wrong email takes as long as
    a wrong password and does not reveal which emails have accounts."""
    return hash_password(secrets.token_urlsafe(32))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAX_MEMORY, dklen=_KEY_BYTES)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
