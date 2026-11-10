import time

import bcrypt
import jwt
import pytest

from seonaut_mcp.auth import Authenticator, AuthError, LoginRateLimiter, bearer_token
from seonaut_mcp.config import Settings

SECRET = "unit-test-secret-0123456789-abcdefghijklmnop"


class FakeDB:
    """Just enough of Database for the authenticator: a users table in memory."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}

    def add(self, user_id: int, email: str, password: str, deleting: int = 0) -> None:
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode()
        self.users[user_id] = {"id": user_id, "email": email, "password": hashed, "deleting": deleting}

    def one(self, sql: str, params: tuple = ()):
        key = params[0]
        for row in self.users.values():
            if row["deleting"]:
                continue
            if ("email = %s" in sql and row["email"] == key) or ("id = %s" in sql and row["id"] == key):
                return {k: row[k] for k in ("id", "email", "password")}
        return None


@pytest.fixture
def db() -> FakeDB:
    fake = FakeDB()
    fake.add(1, "owner@example.com", "right-password")
    return fake


def make_auth(db: FakeDB, **overrides) -> Authenticator:
    settings = Settings(secret=SECRET, user_cache_seconds=0, **overrides)
    return Authenticator(settings, db)


def test_issue_and_verify(db):
    auth = make_auth(db)
    issued = auth.issue("owner@example.com")
    assert issued.expires_at is None
    assert auth.verify(issued.token).email == "owner@example.com"


def test_token_expiry_is_set(db):
    issued = make_auth(db).issue("owner@example.com", days=7)
    assert issued.expires_at and issued.expires_at > time.time() + 6 * 86400


def test_unknown_user_cannot_get_token(db):
    with pytest.raises(AuthError):
        make_auth(db).issue("nobody@example.com")


def test_password_change_invalidates_token(db):
    auth = make_auth(db)
    token = auth.issue("owner@example.com").token
    db.add(1, "owner@example.com", "new-password")
    with pytest.raises(AuthError, match="no longer valid"):
        auth.verify(token)


def test_deleted_user_invalidates_token(db):
    auth = make_auth(db)
    token = auth.issue("owner@example.com").token
    db.users[1]["deleting"] = 1
    with pytest.raises(AuthError):
        auth.verify(token)


def test_revoked_token(db):
    issued = make_auth(db).issue("owner@example.com")
    auth = make_auth(db, revoked_tokens=frozenset({issued.token_id}))
    with pytest.raises(AuthError, match="revoked"):
        auth.verify(issued.token)


def test_expired_token(db):
    auth = make_auth(db)
    claims = jwt.decode(
        auth.issue("owner@example.com").token, SECRET, algorithms=["HS256"], issuer="seonaut-mcp"
    )
    claims["exp"] = int(time.time()) - 10
    with pytest.raises(AuthError, match="expired"):
        auth.verify(jwt.encode(claims, SECRET, algorithm="HS256"))


def test_token_signed_with_other_secret_is_rejected(db):
    token = make_auth(db).issue("owner@example.com").token
    other = Authenticator(Settings(secret="another-secret-0123456789-abcdefghijklmnop"), db)
    with pytest.raises(AuthError, match="Invalid token"):
        other.verify(token)


def test_tampered_subject_is_rejected(db):
    db.add(2, "other@example.com", "x")
    auth = make_auth(db)
    claims = jwt.decode(
        auth.issue("owner@example.com").token, SECRET, algorithms=["HS256"], issuer="seonaut-mcp"
    )
    claims["sub"] = "2"  # re-signed with the real secret: the fingerprint still belongs to user 1
    with pytest.raises(AuthError):
        auth.verify(jwt.encode(claims, SECRET, algorithm="HS256"))


def test_check_password(db):
    auth = make_auth(db)
    assert auth.check_password("owner@example.com", "right-password").id == 1
    with pytest.raises(AuthError):
        auth.check_password("owner@example.com", "wrong")
    with pytest.raises(AuthError):
        auth.check_password("nobody@example.com", "right-password")


def test_go_bcrypt_hash_is_accepted(db):
    # SEOnaut (Go, golang.org/x/crypto/bcrypt) writes $2a$ hashes.
    hashed = bcrypt.hashpw(b"go-password", bcrypt.gensalt(4, prefix=b"2a")).decode()
    assert hashed.startswith("$2a$")
    db.users[3] = {"id": 3, "email": "go@example.com", "password": hashed, "deleting": 0}
    assert make_auth(db).check_password("go@example.com", "go-password").id == 3


def test_rate_limiter():
    limiter = LoginRateLimiter(attempts=2, window_seconds=60)
    assert not limiter.blocked("1.2.3.4")
    limiter.failed("1.2.3.4")
    limiter.failed("1.2.3.4")
    assert limiter.blocked("1.2.3.4")
    assert not limiter.blocked("5.6.7.8")
    limiter.succeeded("1.2.3.4")
    assert not limiter.blocked("1.2.3.4")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": "Bearer abc"}, "abc"),
        ({"Authorization": "bearer  abc "}, "abc"),
        ({"authorization": "Basic abc"}, None),
        ({"authorization": "Bearer "}, None),
        ({}, None),
        (None, None),
    ],
)
def test_bearer_token(headers, expected):
    assert bearer_token(headers) == expected
