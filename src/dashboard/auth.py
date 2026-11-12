"""Dashboard sign-in: analysts from a secrets file of bcrypt hashes.

The users file (``DASHBOARD_USERS_FILE``, default ``secrets/dashboard-users.toml``, gitignored)
is TOML::

    [users]
    alice = "$2b$12$..."

Add or replace a user (prompts for the password; never stores or logs it)::

    python -m src.dashboard.auth add alice [--file secrets/dashboard-users.toml]

The signed-in name is what every review records as ``analyst``. No users file, or an empty one,
means nobody can sign in (fail closed). Existing ``case_reviews`` rows keep their names.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tomllib
from pathlib import Path

import bcrypt

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_USERS_FILE = REPO_ROOT / "secrets" / "dashboard-users.toml"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
MIN_PASSWORD_LENGTH = 10


class UsersFileError(ValueError):
    """The users file is malformed."""


def users_file() -> Path:
    return Path(os.environ.get("DASHBOARD_USERS_FILE") or DEFAULT_USERS_FILE)


def load_users(path: Path | None = None) -> dict[str, str]:
    """``{name: bcrypt hash}``; empty if the file does not exist."""
    path = path or users_file()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise UsersFileError(f"{path}: {exc}") from exc
    users = data.get("users", {})
    if not isinstance(users, dict) or not all(
        isinstance(k, str) and NAME.match(k) and isinstance(v, str) and v.startswith("$2")
        for k, v in users.items()
    ):
        raise UsersFileError(f'{path}: expected [users] name = "<bcrypt hash>" entries')
    return dict(users)


def verify(users: dict[str, str], name: str, password: str) -> bool:
    """True only for a known user with the right password. Unknown users still pay one bcrypt
    check, so the response time does not reveal which names exist."""
    name = name.strip()
    stored = users.get(name)
    candidate = password.encode("utf-8")
    if stored is None:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(candidate, stored.encode("utf-8"))


def hash_password(password: str, rounds: int = 12) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def write_users(path: Path, users: dict[str, str]) -> None:
    lines = ["# Dashboard users: bcrypt hashes only (python -m src.dashboard.auth add NAME).", ""]
    lines.append("[users]")
    lines += [f'{name} = "{h}"' for name, h in sorted(users.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_DUMMY_HASH = bcrypt.hashpw(b"not-a-user-password", bcrypt.gensalt(rounds=4))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="add or replace a user (prompts for the password)")
    add.add_argument("name")
    add.add_argument("--file", type=Path, default=None)
    add.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin (scripts)"
    )
    args = parser.parse_args(argv)
    if not NAME.match(args.name):
        parser.error("name: letters, digits, _ . - (start with a letter, at most 64)")
    path = args.file or users_file()
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"password for {args.name}: ")
        if getpass.getpass("again: ") != password:
            parser.error("passwords differ")
    users = load_users(path)
    existed = args.name in users
    users[args.name] = hash_password(password)
    write_users(path, users)
    print(f"{'replaced' if existed else 'added'} {args.name} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
