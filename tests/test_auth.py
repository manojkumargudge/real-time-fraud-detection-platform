"""S7: API key loading/comparison and the dashboard's DASHBOARD_USERS parsing (no services)."""

from __future__ import annotations

import pytest

from src.dashboard import auth as dash_auth
from src.serving import auth as api_auth

KEY = "k" * 32
PASSWORD = "correct-horse-battery"


# --------------------------------------------------------------------------- API key


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", f"  {KEY}\n")
    assert api_auth.load_api_key() == KEY


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_api_key_configured_is_none(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("API_KEY", raising=False)
    else:
        monkeypatch.setenv("API_KEY", value)
    assert api_auth.load_api_key() is None


def test_short_placeholder_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "change-me")
    with pytest.raises(api_auth.ApiKeyError, match="at least 16") as exc:
        api_auth.load_api_key()
    assert "change-me" not in str(exc.value)


def test_example_placeholder_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    line = next(
        x for x in example.read_text(encoding="utf-8").splitlines() if x.startswith("API_KEY=")
    )
    monkeypatch.setenv("API_KEY", line.split("=", 1)[1])
    with pytest.raises(api_auth.ApiKeyError, match="placeholder"):
        api_auth.load_api_key()


def test_api_key_file_is_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Secrets come from the environment only (S7); API_KEY_FILE is ignored."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("API_KEY_FILE", "/nonexistent/should-not-be-read")
    assert api_auth.load_api_key() is None


@pytest.mark.parametrize(
    ("expected", "given", "ok"),
    [
        (KEY, KEY, True),
        (KEY, KEY[:-1] + "x", False),
        (KEY, KEY + "x", False),
        (KEY, "", False),
        (KEY, None, False),
        (None, KEY, False),
        (None, None, False),
    ],
)
def test_key_matches(expected: str | None, given: str | None, ok: bool) -> None:
    assert api_auth.key_matches(expected, given) is ok


def test_key_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    import hmac

    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)  # api_auth calls hmac.compare_digest
    assert api_auth.key_matches(KEY, KEY)
    assert calls == [(KEY.encode(), KEY.encode())]


# --------------------------------------------------------------------------- dashboard users


@pytest.fixture(scope="module")
def alice_entry() -> str:
    return dash_auth.entry("alice", PASSWORD, rounds=4)


def test_entry_is_name_and_bcrypt_hash_never_the_password(alice_entry: str) -> None:
    name, _, hashed = alice_entry.partition(":")
    assert name == "alice" and hashed.startswith("$2b$04$")
    assert PASSWORD not in alice_entry


def test_parse_users_and_verify(alice_entry: str) -> None:
    bob = dash_auth.entry("bob", "another-password-9", rounds=4)
    users = dash_auth.parse_users(f" {alice_entry} , {bob} ,")
    assert set(users) == {"alice", "bob"}
    assert dash_auth.verify(users, "alice", PASSWORD)
    assert dash_auth.verify(users, " alice ", PASSWORD)
    assert not dash_auth.verify(users, "alice", "another-password-9")
    assert not dash_auth.verify(users, "mallory", PASSWORD)


def test_load_users_reads_the_env(monkeypatch: pytest.MonkeyPatch, alice_entry: str) -> None:
    monkeypatch.setenv("DASHBOARD_USERS", alice_entry)
    assert list(dash_auth.load_users()) == ["alice"]
    monkeypatch.delenv("DASHBOARD_USERS")
    assert dash_auth.load_users() == {}


@pytest.mark.parametrize(
    "raw",
    [
        "alice",  # no hash
        "alice:plain-text-password",  # not a bcrypt hash
        "1alice:$2b$04$" + "a" * 53,  # bad name
        "alice:$2b$04$" + "a" * 52,  # truncated hash
    ],
)
def test_malformed_entries_are_rejected_without_echoing_them(raw: str) -> None:
    with pytest.raises(dash_auth.UsersConfigError) as exc:
        dash_auth.parse_users(raw)
    assert "plain-text-password" not in str(exc.value) and "$2b$" not in str(exc.value)


def test_duplicate_user_is_rejected(alice_entry: str) -> None:
    with pytest.raises(dash_auth.UsersConfigError, match="twice"):
        dash_auth.parse_users(f"{alice_entry},{alice_entry}")


def test_short_password_and_bad_name_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        dash_auth.entry("alice", "short", rounds=4)
    with pytest.raises(ValueError, match="name"):
        dash_auth.entry("bad name", PASSWORD, rounds=4)


def test_hash_cli_prints_only_the_entry(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert dash_auth.main(["hash", "alice", "--password-stdin"]) == 0
    out = capsys.readouterr().out.strip()
    assert PASSWORD not in out
    users = dash_auth.parse_users(out)
    assert dash_auth.verify(users, "alice", PASSWORD)
