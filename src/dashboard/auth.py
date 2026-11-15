"""Dashboard sign-in: analysts and their bcrypt password hashes from the environment.

``DASHBOARD_USERS`` holds comma-separated ``name:<bcrypt hash>`` entries (Compose: the
gitignored ``.env``, single-quoted so ``$`` in the hashes is not interpolated; Kubernetes:
Secret ``fraud-auth``, key ``dashboard-users``). Only hashes are stored, never passwords.

Make an entry (prompts for the password; prints ``name:<hash>``, never the password)::

    python -m src.dashboard.auth hash alice

The signed-in name is what every review records as ``analyst``. No users, or an empty value,
means nobody can sign in (fail closed). Existing ``case_reviews`` rows keep their names.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

import bcrypt

ENV_VAR = "DASHBOARD_USERS"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
MIN_PASSWORD_LENGTH = 10
_HASH = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")


class UsersConfigError(ValueError):
    """``DASHBOARD_USERS`` is malformed (the message never contains a hash)."""


def parse_users(raw: str) -> dict[str, str]:
    """``{name: bcrypt hash}`` from ``name:hash[,name:hash...]``; empty for an empty value."""
    users: dict[str, str] = {}
    for i, entry in enumerate(e.strip() for e in raw.split(",")):
        if not entry:
            continue
        name, sep, hashed = entry.partition(":")
        name, hashed = name.strip(), hashed.strip()
        if not sep or not NAME.match(name) or not _HASH.match(hashed):
            raise UsersConfigError(
                f"{ENV_VAR} entry {i + 1}: expected name:<bcrypt hash> "
                "(python -m src.dashboard.auth hash NAME)"
            )
        if name in users:
            raise UsersConfigError(f"{ENV_VAR}: user {name!r} appears twice")
        users[name] = hashed
    return users


def load_users() -> dict[str, str]:
    return parse_users(os.environ.get(ENV_VAR, ""))


def verify(users: dict[str, str], name: str, password: str) -> bool:
    """True only for a known user with the right password. Unknown users still pay one bcrypt
    check, so the response time does not reveal which names exist."""
    stored = users.get(name.strip())
    candidate = password.encode("utf-8")
    if stored is None:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(candidate, stored.encode("utf-8"))


def hash_password(password: str, rounds: int = 12) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def entry(name: str, password: str, rounds: int = 12) -> str:
    """One ``DASHBOARD_USERS`` entry, ``name:<bcrypt hash>``."""
    if not NAME.match(name):
        raise ValueError("name: letters, digits, _ . - (start with a letter, at most 64)")
    return f"{name}:{hash_password(password, rounds)}"


_DUMMY_HASH = bcrypt.hashpw(b"not-a-user-password", bcrypt.gensalt(rounds=4))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_hash = sub.add_parser("hash", help="print a DASHBOARD_USERS entry (prompts for password)")
    p_hash.add_argument("name")
    p_hash.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin (scripts)"
    )
    args = parser.parse_args(argv)
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"password for {args.name}: ")
        if getpass.getpass("again: ") != password:
            parser.error("passwords differ")
    try:
        print(entry(args.name, password))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
