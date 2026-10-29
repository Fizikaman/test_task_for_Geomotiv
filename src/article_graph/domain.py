"""Eagerly registered DTOs. No persistence, authentication or provider I/O."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from article_graph.settings import Limits

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
LocalID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$")]
RelationKind = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Status(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    REFRESH_REUSED = "refresh_reused"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RETRY_CONFLICT = "retry_conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INVALID_DOCUMENT = "invalid_document"
    LIMIT_EXCEEDED = "limit_exceeded"
    EXTRACTION_INVALID = "extraction_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROCESSING_TIMEOUT = "processing_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


class SafeError(DTO):
    # Messages are chosen by the service, never copied from a provider or document.
    code: ErrorCode
    message: Text


class ErrorResponse(DTO):
    error: SafeError


class Revision(DTO):
    article_id: UUID
    revision_id: UUID
    number: int = Field(ge=1)
    content_hash: ContentHash


class Job(DTO):
    article_id: UUID
    revision_id: UUID
    job_id: UUID
    status: Status
    attempt: int = Field(default=0, ge=0)
    error: SafeError | None = None

    @model_validator(mode="after")
    def failure_details(self) -> Self:
        if (self.status == Status.FAILED) != (self.error is not None):
            raise ValueError("only failed jobs require an error")
        return self


class Article(DTO):
    article_id: UUID
    latest_revision_id: UUID
    active_revision_id: UUID | None = None
    job_id: UUID
    status: Literal["queued", "processing", "completed", "failed"]
    error: SafeError | None = None

    @model_validator(mode="after")
    def failure_details(self) -> Self:
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("only failed articles require an error")
        if (
            self.status == "completed"
            and self.active_revision_id != self.latest_revision_id
        ):
            raise ValueError("completed latest revision must be active")
        return self


class ProcessingResponse(DTO):
    article_id: UUID
    revision_id: UUID
    job_id: UUID
    status: Literal["queued", "processing", "completed", "failed"]
    status_url: Annotated[str, StringConstraints(pattern=r"^/articles/[0-9a-f-]{36}$")]
    unchanged: bool = False

    @model_validator(mode="after")
    def matching_status_url(self) -> Self:
        if self.status_url != f"/articles/{self.article_id}":
            raise ValueError("status_url must identify the article")
        return self


class TextEvidence(DTO):
    kind: Literal["text"] = "text"
    fragment_id: Text
    page: int | None = Field(default=None, ge=1)
    quote: Text


class VisualEvidence(DTO):
    kind: Literal["visual"] = "visual"
    fragment_id: Text
    page: int = Field(ge=1)
    description: Text


Evidence = Annotated[TextEvidence | VisualEvidence, Field(discriminator="kind")]


class ExtractedCharacter(DTO):
    local_id: LocalID
    name: Text
    description: Text
    aliases: tuple[Text, ...] = ()


class Character(DTO):
    character_id: UUID
    name: Text
    description: Text
    aliases: tuple[Text, ...] = ()


class RelationType(DTO):
    kind: RelationKind
    directed: bool

    @model_validator(mode="after")
    def known_direction(self) -> Self:
        if self.kind == "FRIEND_OF" and self.directed:
            raise ValueError("FRIEND_OF is symmetric")
        if self.kind == "PARENT_OF" and not self.directed:
            raise ValueError("PARENT_OF is directed")
        return self


class ExtractedRelation(RelationType):
    local_id: LocalID
    subject_id: LocalID
    object_id: LocalID
    # Only explicit positive assertions enter the graph. No confidence threshold
    # can turn a negation or an uncertain statement into a positive assertion.
    polarity: Literal["affirmed"]
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Relation(RelationType):
    relation_id: UUID
    article_id: UUID
    revision_id: UUID
    subject_id: UUID
    object_id: UUID
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Extraction(DTO):
    characters: tuple[ExtractedCharacter, ...]
    relations: tuple[ExtractedRelation, ...]

    @model_validator(mode="after")
    def local_references(self) -> Self:
        character_ids = {item.local_id for item in self.characters}
        relation_ids = {item.local_id for item in self.relations}
        if len(character_ids) != len(self.characters):
            raise ValueError("duplicate character local_id")
        if len(relation_ids) != len(self.relations):
            raise ValueError("duplicate relation local_id")
        for relation in self.relations:
            if not {relation.subject_id, relation.object_id} <= character_ids:
                raise ValueError("relation references an unknown character local_id")
        return self

    def check_limits(self, limits: Limits) -> Self:
        """Required after parsing at the provider boundary, before persistence."""
        if len(self.characters) > limits.characters:
            raise ValueError("character limit exceeded")
        if len(self.relations) > limits.relations:
            raise ValueError("relation limit exceeded")
        if any(
            len(item.evidence) > limits.evidence_per_relation for item in self.relations
        ):
            raise ValueError("evidence limit exceeded")
        return self


class PageRequest(DTO):
    limit: int = Field(gt=0)
    offset: int = Field(default=0, ge=0)

    def check_limits(self, limits: Limits) -> Self:
        if self.limit > limits.page_max:
            raise ValueError("page limit exceeded")
        return self


class Page[T](DTO):
    items: tuple[T, ...]
    limit: int = Field(gt=0)
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
