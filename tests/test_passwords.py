"""Passwords of admin panel accounts: only a salted scrypt hash is stored, a damaged hash never matches."""

import pytest

from medllmbot.admin.passwords import dummy_hash, hash_password, verify_password

PASSWORD = "correct-horse-battery"


def test_hash_verifies_the_password_only() -> None:
    stored = hash_password(PASSWORD)

    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD.upper(), stored)
    assert not verify_password("", stored)


def test_hash_keeps_its_parameters_and_hides_the_password() -> None:
    stored = hash_password(PASSWORD)

    assert stored.startswith("scrypt$16384$8$1$")
    assert PASSWORD not in stored


def test_same_password_gets_a_new_salt_every_time() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not a hash",
        "bcrypt$16384$8$1$c2FsdA==$a2V5",  # another scheme
        "scrypt$16384$8$1$!!!$a2V5",  # salt is not base64
        "scrypt$abc$8$1$c2FsdA==$a2V5",  # parameter is not a number
        "scrypt$3$8$1$c2FsdA==$a2V5",  # scrypt refuses n that is not a power of two
    ],
)
def test_damaged_hash_never_matches(stored: str) -> None:
    assert not verify_password(PASSWORD, stored)


def test_dummy_hash_is_computed_once_and_matches_nothing() -> None:
    assert dummy_hash() is dummy_hash()
    assert not verify_password("", dummy_hash())
