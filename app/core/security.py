import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

# Checked when an email is unknown, so a missing account takes about as long as a wrong password.
_DUMMY_HASH = _hasher.hash("dummy password used only to equalise login timing")


def _hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify_password(password: str, hashed_password: str) -> bool:
    try:
        return _hasher.verify(hashed_password, password)
    except (VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_password, password)


async def verify_password(password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(_verify_password, password, hashed_password)


async def verify_against_dummy_hash(password: str) -> None:
    await verify_password(password, _DUMMY_HASH)
