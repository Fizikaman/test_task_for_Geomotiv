import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import run
from threading import Barrier
from time import time
from typing import Literal
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from neo4j import Driver
from neo4j.exceptions import ConstraintError, ServiceUnavailable
from pydantic import SecretStr, ValidationError

from article_graph.app import create_app
from article_graph.auth import (
    ALGORITHM,
    AuthError,
    Claims,
    Purpose,
    TokenPair,
    decode_token,
    issue_pair,
    signing_key,
    verify_app_token,
)
from article_graph.auth_routes import Access
from article_graph.refresh_sessions import RefreshSessions, token_hash
from article_graph.settings import Neo4jSettings, Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_"):
            monkeypatch.delenv(name)
    return Settings(
        _env_file=None,
        app_token="test-app-token-" * 4,
        jwt_access_key="test-access-key-" * 4,
        jwt_refresh_key="test-refresh-key-" * 4,
        app_id=f"p04-test-{uuid4()}",
    )


def protected_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/test/protected")
    def protected(access: Access) -> dict[str, str]:
        return {"app_id": access.sub}

    return app


@pytest.fixture
def client(settings: Settings) -> TestClient:
    # Unit tests don't start lifespan or create a driver.
    return TestClient(protected_app(settings))


@pytest.fixture
def db_client(
    settings: Settings, neo4j_settings: Neo4jSettings, neo4j_driver: Driver
) -> Iterator[TestClient]:
    settings.neo4j = neo4j_settings
    try:
        with TestClient(protected_app(settings)) as client:
            yield client
    finally:
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            # The fixture is hardwired to the isolated test DB. Delete only this app.
            session.run(
                "MATCH (s:RefreshSession {app_id: $app}) DETACH DELETE s",
                app=settings.app_id,
            ).consume()


def encoded(
    settings: Settings,
    purpose: Purpose = "access",
    *,
    changes: dict[str, object] | None = None,
    remove: str | None = None,
    algorithm: str = ALGORITHM,
    key: str | None = None,
) -> str:
    pair = issue_pair(settings)
    source = (
        pair.tokens.access_token if purpose == "access" else pair.tokens.refresh_token
    )
    payload: dict[str, object] = decode_token(source, purpose, settings).model_dump()
    payload.update(changes or {})
    if remove is not None:
        payload.pop(remove)
    return jwt.encode(
        payload,
        signing_key(settings, purpose) if key is None else key,
        algorithm=algorithm,
    )


def assert_denied(client: TestClient, token: str, purpose: Purpose = "access") -> None:
    response = (
        client.get("/test/protected", headers={"Authorization": f"Bearer {token}"})
        if purpose == "access"
        else client.post("/auth/refresh", json={"refresh_token": token})
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Authentication failed"}
    }
    assert token not in response.text


def exchange(client: TestClient, settings: Settings) -> TokenPair:
    response = client.post(
        "/auth/token", json={"app_token": settings.app_token.get_secret_value()}
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    return TokenPair.model_validate(response.json())


def test_required_independent_keys(settings: Settings) -> None:
    values = settings.model_dump()
    for field in ("jwt_access_key", "jwt_refresh_key"):
        without = {k: v for k, v in values.items() if k != field}
        with pytest.raises(ValidationError):
            Settings.model_validate(without)
        for invalid in ("short", settings.app_token.get_secret_value()):
            with pytest.raises(ValidationError) as error:
                Settings.model_validate({**values, field: invalid})
            assert "input_value" not in str(error.value)
    with pytest.raises(ValidationError):
        Settings.model_validate({**values, "jwt_refresh_key": settings.jwt_access_key})


def test_issuing_claims_ttl_and_redacted_repr(settings: Settings) -> None:
    pair = issue_pair(settings)
    access = decode_token(pair.tokens.access_token, "access", settings)
    refresh = decode_token(pair.tokens.refresh_token, "refresh", settings)
    assert access.sub == settings.app_id
    assert access.exp - access.iat == pair.tokens.expires_in == 900
    assert refresh.exp - refresh.iat == 86400
    assert access.jti != refresh.jti
    assert access.family_id == refresh.family_id == refresh.jti
    assert pair.tokens.access_token not in repr(pair)
    assert pair.tokens.refresh_token not in repr(pair)


def test_unicode_app_token(settings: Settings) -> None:
    settings.app_token = SecretStr("тестовый-токен-" * 4)
    verify_app_token(settings.app_token, settings)
    with pytest.raises(AuthError):
        verify_app_token(SecretStr("неверный"), settings)


@pytest.mark.parametrize("bad", ["wrong-app-token", "пароль", "x" * 32])
def test_bad_app_token(client: TestClient, bad: str) -> None:
    response = client.post("/auth/token", json={"app_token": bad})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert bad not in response.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/auth/token", {}),
        ("/auth/token", {"app_token": "", "secret-extra": "do-not-echo"}),
        ("/auth/token", {"app_token": {"secret": "do-not-echo"}}),
        ("/auth/refresh", {"refresh_token": "do-not-echo", "extra": "secret"}),
        ("/auth/refresh", {"refresh_token": 123}),
        ("/auth/refresh", {"refresh_token": "x" * 8193}),
    ],
)
def test_safe_validation(
    client: TestClient, path: str, body: dict[str, object]
) -> None:
    response = client.post(path, json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Invalid request"}
    }
    response = client.post(path, content='{"secret":"do-not-echo",invalid-json')
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer", "Bearer invalid"])
def test_access_required(client: TestClient, header: str | None) -> None:
    response = client.get(
        "/test/protected", headers={} if header is None else {"Authorization": header}
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_access_and_public_endpoints(client: TestClient, settings: Settings) -> None:
    pair = issue_pair(settings)
    response = client.get(
        "/test/protected",
        headers={"Authorization": f"Bearer {pair.tokens.access_token}"},
    )
    assert response.json() == {"app_id": settings.app_id}
    assert client.get("/health/live").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/test/protected"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "/auth/token" in schema["paths"]
    assert pair.tokens.access_token not in str(schema)


@pytest.mark.parametrize("purpose", ["access", "refresh"])
@pytest.mark.parametrize(
    "changes",
    [
        {"exp": 1},
        {"iss": "other-issuer"},
        {"aud": "other-audience"},
        {"aud": ["article-graph-api"]},
        {"sub": "other-app"},
        {"purpose": "invalid"},
        {"jti": "not-a-uuid"},
        {"family_id": "not-a-uuid"},
        {"exp": "9999999999"},
        {"exp": float("inf")},
        {"iat": float("inf")},
        {"iat": True},
        {"nbf": 9999999999},
        {"iat": 9999999999},
        {"extra": "unexpected"},
    ],
)
def test_invalid_claims(
    client: TestClient, settings: Settings, purpose: Purpose, changes: dict[str, object]
) -> None:
    assert_denied(client, encoded(settings, purpose, changes=changes), purpose)


@pytest.mark.parametrize("purpose", ["access", "refresh"])
@pytest.mark.parametrize("missing", list(Claims.model_fields))
def test_missing_claims(
    client: TestClient, settings: Settings, purpose: Purpose, missing: str
) -> None:
    assert_denied(client, encoded(settings, purpose, remove=missing), purpose)


@pytest.mark.parametrize("purpose", ["access", "refresh"])
def test_tampered_algorithm_key_and_purpose(
    client: TestClient, settings: Settings, purpose: Purpose
) -> None:
    opposite: Purpose = "refresh" if purpose == "access" else "access"
    assert_denied(client, encoded(settings, opposite), purpose)
    # Valid signature with the EXPECTED key, but a wrong purpose claim.
    assert_denied(
        client, encoded(settings, purpose, changes={"purpose": opposite}), purpose
    )
    assert_denied(client, encoded(settings, purpose, key="other-key-" * 4), purpose)
    assert_denied(client, encoded(settings, purpose, algorithm="HS384"), purpose)
    assert_denied(client, encoded(settings, purpose, algorithm="none", key=""), purpose)
    raw = encoded(settings, purpose)
    header, payload, signature = raw.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert_denied(client, f"{header}.{payload}.{signature}", purpose)
    assert_denied(client, "malformed.jwt", purpose)


def test_storage_unavailable_is_safe(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post(
        "/auth/token", json={"app_token": settings.app_token.get_secret_value()}
    )
    assert response.status_code == 503

    def fail_create(self: RefreshSessions, pair: object) -> None:
        raise ServiceUnavailable("secret-driver-message")

    def fail_rotate(self: RefreshSessions, *args: object) -> Literal["ok"]:
        raise ServiceUnavailable("secret-driver-message")

    assert isinstance(client.app, FastAPI)
    client.app.state.driver = object()
    monkeypatch.setattr(RefreshSessions, "create", fail_create)
    monkeypatch.setattr(RefreshSessions, "rotate", fail_rotate)
    for path, body in (
        ("/auth/token", {"app_token": settings.app_token.get_secret_value()}),
        ("/auth/refresh", {"refresh_token": issue_pair(settings).tokens.refresh_token}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert "secret-driver-message" not in response.text


def test_jwt_lazy_runtime() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import article_graph.app\n"
            "assert 'jwt' not in sys.modules\n"
            "from article_graph.settings import Settings\n"
            "from article_graph.auth import issue_pair, decode_token\n"
            "s = Settings(_env_file=None, app_token='app-' * 10, "
            "jwt_access_key='access-' * 10, jwt_refresh_key='refresh-' * 10)\n"
            "p = issue_pair(s)\n"
            "assert 'jwt' in sys.modules\n"
            "assert decode_token(p.tokens.access_token, 'access', s).purpose == 'access'\n",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
def test_rotation_replay_and_hash_storage(
    db_client: TestClient,
    settings: Settings,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    initial = exchange(db_client, settings)
    response = db_client.post(
        "/auth/refresh", json={"refresh_token": initial.refresh_token}
    )
    assert response.status_code == 200
    rotated = TokenPair.model_validate(response.json())
    assert rotated.access_token != initial.access_token
    assert rotated.refresh_token != initial.refresh_token
    assert response.headers["Cache-Control"] == "no-store"
    initial_claims = decode_token(initial.refresh_token, "refresh", settings)
    rotated_claims = decode_token(rotated.refresh_token, "refresh", settings)
    assert initial_claims.family_id == rotated_claims.family_id
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        nodes = [
            dict(row["s"])
            for row in session.run(
                "MATCH (s:RefreshSession {app_id: $app}) RETURN s", app=settings.app_id
            )
        ]
    assert len(nodes) == 2
    assert {node["token_hash"] for node in nodes} == {
        token_hash(initial.refresh_token),
        token_hash(rotated.refresh_token),
    }
    assert sorted(node["used"] for node in nodes) == [False, True]
    for raw in (initial.refresh_token, rotated.refresh_token, initial.access_token):
        assert raw not in str(nodes)
    replay = db_client.post(
        "/auth/refresh", json={"refresh_token": initial.refresh_token}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_reused"
    assert_denied(db_client, rotated.refresh_token, "refresh")
    # Access remains valid until exp, as defined by the contract.
    assert (
        db_client.get(
            "/test/protected",
            headers={"Authorization": f"Bearer {initial.access_token}"},
        ).status_code
        == 200
    )
    for secret in (
        settings.app_token.get_secret_value(),
        settings.jwt_access_key.get_secret_value(),
        settings.jwt_refresh_key.get_secret_value(),
        initial.access_token,
        initial.refresh_token,
        rotated.refresh_token,
    ):
        assert secret not in caplog.text
    # A different family of the same app is unaffected.
    other = exchange(db_client, settings)
    assert (
        db_client.post(
            "/auth/refresh", json={"refresh_token": other.refresh_token}
        ).status_code
        == 200
    )


@pytest.mark.integration
@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_refresh(
    db_client: TestClient, settings: Settings, iteration: int
) -> None:
    pair = exchange(db_client, settings)
    barrier = Barrier(2)

    def refresh() -> tuple[int, dict[str, object]]:
        barrier.wait(timeout=10)
        response = db_client.post(
            "/auth/refresh", json={"refresh_token": pair.refresh_token}
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(status for status, _ in results) == [200, 401]
    winner = next(body for status, body in results if status == 200)
    loser = next(body for status, body in results if status == 401)
    assert loser["error"] == {
        "code": "refresh_reused",
        "message": "Authentication failed",
    }
    successor = TokenPair.model_validate(winner)
    assert_denied(db_client, successor.refresh_token, "refresh")


@pytest.mark.integration
def test_invalid_refresh_cannot_revoke_family(
    db_client: TestClient, settings: Settings
) -> None:
    pair = exchange(db_client, settings)
    claims = decode_token(pair.refresh_token, "refresh", settings)
    for bad in (
        encoded(settings, "refresh", changes={"family_id": claims.family_id}),
        encoded(
            settings,
            "refresh",
            changes={"family_id": claims.family_id, "jti": claims.jti, "exp": 1},
        ),
        encoded(
            settings,
            "refresh",
            changes={"family_id": claims.family_id, "jti": claims.jti},
            key="wrong-key-" * 4,
        ),
    ):
        assert_denied(db_client, bad, "refresh")
    assert (
        db_client.post(
            "/auth/refresh", json={"refresh_token": pair.refresh_token}
        ).status_code
        == 200
    )


@pytest.mark.integration
def test_stored_expiration(
    db_client: TestClient,
    settings: Settings,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
) -> None:
    pair = exchange(db_client, settings)
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        session.run(
            "MATCH (s:RefreshSession {app_id: $app}) SET s.expires_at = $expired",
            app=settings.app_id,
            expired=int(time()) - 1,
        ).consume()
    assert_denied(db_client, pair.refresh_token, "refresh")


@pytest.mark.integration
def test_rotation_rolls_back_consumption_if_successor_cannot_be_created(
    db_client: TestClient,
    settings: Settings,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
) -> None:
    pair = exchange(db_client, settings)
    old = decode_token(pair.refresh_token, "refresh", settings)
    duplicate = issue_pair(settings, old.family_id)
    store = RefreshSessions(neo4j_driver, neo4j_settings)
    store.create(duplicate)
    with pytest.raises(ConstraintError):
        store.rotate(old, pair.refresh_token, duplicate)
    # The failed transaction did not burn the old token.
    assert (
        db_client.post(
            "/auth/refresh", json={"refresh_token": pair.refresh_token}
        ).status_code
        == 200
    )


@pytest.mark.integration
def test_ancestor_replay_serializes_with_descendant_rotation(
    db_client: TestClient,
    settings: Settings,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
) -> None:
    first = exchange(db_client, settings)
    rotated = db_client.post(
        "/auth/refresh", json={"refresh_token": first.refresh_token}
    )
    assert rotated.status_code == 200
    second = TokenPair.model_validate(rotated.json())
    barrier = Barrier(2)

    def refresh(raw: str) -> tuple[int, TokenPair | None]:
        barrier.wait(timeout=10)
        response = db_client.post("/auth/refresh", json={"refresh_token": raw})
        return response.status_code, (
            TokenPair.model_validate(response.json())
            if response.status_code == 200
            else None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replay = pool.submit(refresh, first.refresh_token)
        rotation = pool.submit(refresh, second.refresh_token)
        assert replay.result(timeout=20)[0] == 401
        status, successor = rotation.result(timeout=20)
    assert status in (200, 401)
    if successor is not None:
        assert_denied(db_client, successor.refresh_token, "refresh")
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        rows = list(
            session.run(
                "MATCH (s:RefreshSession {app_id: $app}) RETURN s.revoked AS revoked",
                app=settings.app_id,
            )
        )
        assert rows and all(row["revoked"] for row in rows)
