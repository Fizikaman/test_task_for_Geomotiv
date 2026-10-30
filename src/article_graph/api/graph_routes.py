"""Eager graph route/schema registration; database calls run in the thread pool."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from article_graph.api.auth_routes import Access, Configuration
from article_graph.db.graph_repository import GraphRepository
from article_graph.documents.document_format import DocumentError
from article_graph.domain import (
    Character,
    ErrorCode,
    Page,
    PageRequest,
    Relation,
    RelationKind,
    RelationType,
)

router = APIRouter(tags=["graph"])


def pagination(
    settings: Configuration,
    limit: Annotated[int | None, Query(gt=0)] = None,
    offset: Annotated[int, Query(ge=0, le=(1 << 63) - 1)] = 0,
) -> PageRequest:
    value = settings.limits.page_default if limit is None else limit
    if value > settings.limits.page_max:
        raise DocumentError(ErrorCode.VALIDATION_ERROR)
    return PageRequest(limit=value, offset=offset)


Pagination = Annotated[PageRequest, Depends(pagination)]


def repository(
    request: Request, response: Response, settings: Configuration
) -> GraphRepository:
    response.headers["Cache-Control"] = "no-store"
    if request.app.state.driver is None:
        raise DocumentError(ErrorCode.SERVICE_UNAVAILABLE)
    return GraphRepository(request.app.state.driver, settings.neo4j)


Repository = Annotated[GraphRepository, Depends(repository)]


@router.get("/characters", response_model=Page[Character])
def characters(access: Access, repo: Repository, page: Pagination) -> Page[Character]:
    return repo.characters(page)


@router.get("/characters/by_article/{article_id}", response_model=Page[Character])
def by_article(
    article_id: UUID, access: Access, repo: Repository, page: Pagination
) -> Page[Character]:
    return repo.characters(page, article_id)


@router.get("/relations/types", response_model=Page[RelationType])
def types(access: Access, repo: Repository, page: Pagination) -> Page[RelationType]:
    return repo.types(page)


@router.get("/relations/connected/{character_id}", response_model=Page[Relation])
def connected(
    character_id: UUID,
    access: Access,
    repo: Repository,
    page: Pagination,
    kind: Annotated[RelationKind | None, Query(alias="type")] = None,
) -> Page[Relation]:
    return repo.connected(character_id, kind, page)
