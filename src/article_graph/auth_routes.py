"""Eager router registration and the shared dependency for business routers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
lazy from neo4j.exceptions import DriverError, Neo4jError

from article_graph.auth import (
    AppTokenRequest,
    AuthError,
    Claims,
    RefreshRequest,
    TokenPair,
    decode_token,
    issue_pair,
    verify_app_token,
)
from article_graph.domain import ErrorCode
from article_graph.refresh_sessions import RefreshSessions
from article_graph.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


Configuration = Annotated[Settings, Depends(get_settings)]


def require_access(
    settings: Configuration,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Claims:
    if credentials is None:
        raise AuthError()
    return decode_token(credentials.credentials, "access", settings)


Access = Annotated[Claims, Depends(require_access)]


def get_refresh_sessions(request: Request, settings: Configuration) -> RefreshSessions:
    driver = request.app.state.driver
    if driver is None:
        raise AuthError(ErrorCode.SERVICE_UNAVAILABLE, 503)
    return RefreshSessions(driver, settings.neo4j)


@router.post("/token", response_model=TokenPair)
def token(
    body: AppTokenRequest,
    response: Response,
    settings: Configuration,
    request: Request,
) -> TokenPair:
    verify_app_token(body.app_token, settings)
    pair = issue_pair(settings)
    try:
        get_refresh_sessions(request, settings).create(pair)
    except Neo4jError, DriverError:
        raise AuthError(ErrorCode.SERVICE_UNAVAILABLE, 503) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return pair.tokens


@router.post("/refresh", response_model=TokenPair)
def refresh(
    body: RefreshRequest,
    response: Response,
    settings: Configuration,
    request: Request,
) -> TokenPair:
    raw = body.refresh_token.get_secret_value()
    claims = decode_token(raw, "refresh", settings)
    pair = issue_pair(settings, claims.family_id)
    try:
        result = get_refresh_sessions(request, settings).rotate(claims, raw, pair)
    except Neo4jError, DriverError:
        raise AuthError(ErrorCode.SERVICE_UNAVAILABLE, 503) from None
    if result != "ok":
        raise AuthError(ErrorCode(result))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return pair.tokens
