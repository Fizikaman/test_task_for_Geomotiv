"""JWT verification and issuing. No tokens or credentials in exception messages."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from time import time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
lazy import jwt

from article_graph.domain import DTO, ErrorCode
from article_graph.settings import Settings

ALGORITHM = "HS256"
type Purpose = Literal["access", "refresh"]


class AuthError(Exception):
    def __init__(
        self, code: ErrorCode = ErrorCode.UNAUTHORIZED, status_code: int = 401
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.status_code = status_code


class AppTokenRequest(DTO):
    app_token: SecretStr = Field(min_length=1)


class RefreshRequest(DTO):
    refresh_token: SecretStr = Field(min_length=1, max_length=8192)


class TokenPair(DTO):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class Claims(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    iss: str
    aud: str
    sub: str
    jti: str
    family_id: str
    purpose: Purpose
    iat: int
    nbf: int
    exp: int


@dataclass(frozen=True)
class IssuedPair:
    tokens: TokenPair
    refresh: Claims


def verify_app_token(token: SecretStr, settings: Settings) -> None:
    # Comparing fixed-length digests also accepts Unicode credentials safely.
    if not compare_digest(
        sha256(token.get_secret_value().encode()).digest(),
        sha256(settings.app_token.get_secret_value().encode()).digest(),
    ):
        raise AuthError()


def signing_key(settings: Settings, purpose: Purpose) -> str:
    key = settings.jwt_access_key if purpose == "access" else settings.jwt_refresh_key
    return key.get_secret_value()


def decode_token(token: str, purpose: Purpose, settings: Settings) -> Claims:
    if len(token) > 8192:
        raise AuthError()
    try:
        payload = jwt.decode(
            token,
            signing_key(settings, purpose),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            subject=settings.app_id,
            options={
                "require": list(Claims.model_fields),
                "strict_aud": True,
            },
        )
        claims = Claims.model_validate(payload)
        if (
            claims.purpose != purpose
            or claims.exp <= claims.iat
            or claims.nbf != claims.iat
            or str(UUID(claims.jti)) != claims.jti
            or str(UUID(claims.family_id)) != claims.family_id
        ):
            raise AuthError()
        return claims
    except jwt.InvalidTokenError, ValidationError, ValueError, TypeError, OverflowError:
        raise AuthError() from None


def issue_pair(settings: Settings, family_id: str | None = None) -> IssuedPair:
    now = int(time())
    refresh_id = str(uuid4())
    family = family_id if family_id is not None else refresh_id

    def claims(purpose: Purpose, ttl: int, token_id: str) -> Claims:
        return Claims(
            iss=settings.jwt_issuer,
            aud=settings.jwt_audience,
            sub=settings.app_id,
            jti=token_id,
            family_id=family,
            purpose=purpose,
            iat=now,
            nbf=now,
            exp=now + ttl,
        )

    access = claims("access", settings.limits.access_ttl_seconds, str(uuid4()))
    refresh = claims("refresh", settings.limits.refresh_ttl_seconds, refresh_id)
    return IssuedPair(
        tokens=TokenPair(
            access_token=jwt.encode(
                access.model_dump(),
                signing_key(settings, "access"),
                algorithm=ALGORITHM,
            ),
            refresh_token=jwt.encode(
                refresh.model_dump(),
                signing_key(settings, "refresh"),
                algorithm=ALGORITHM,
            ),
            expires_in=settings.limits.access_ttl_seconds,
        ),
        refresh=refresh,
    )
