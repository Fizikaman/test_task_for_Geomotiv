"""Application pipeline. The caller owns claim, heartbeat and retry scheduling."""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
lazy from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from article_graph.blob_store import BlobNamespace, BlobStore
from article_graph.document_format import DocumentError, DocumentFormat
from article_graph.domain import DTO, ContentHash, ErrorCode, Extraction, RelationType
from article_graph.embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingMetadata,
    Embeddings,
)
from article_graph.extraction import (
    ExtractionArtifact,
    ExtractionProvider,
    extract_document,
)
from article_graph.llm import (
    LLM,
    GenerationRequest,
    GenerationResult,
    ImageBlock,
    LLMError,
    TextBlock,
)
from article_graph.pdf_preparation import PreparedPdf, prepare_pdf
from article_graph.pipeline_checkpoints import (
    Attempt,
    CheckpointRepository,
    PipelineError,
    PipelineStopped,
    Pointer,
    RevisionInput,
    Stage,
)
from article_graph.preparation import PreparedDocument, prepare_markdown
from article_graph.publication import (
    GraphPublisher,
    Publication,
    PublicationError,
    PublicationResult,
)
from article_graph.resolution import (
    CandidateStore,
    IdentityVerdict,
    ResolutionArtifact,
    resolve,
)
from article_graph.settings import Limits, LLMSettings, OllamaSettings


class PipelineLLM(LLM, ExtractionProvider, Protocol):
    pass


class Versions(DTO):
    pipeline: str = "pipeline-v1"
    preparation: str = "text-v1/pdfium-5.14.0-v1"
    extraction: str = "extraction-v1/document-v1"
    resolution: str = "resolution-v1/identity-v1"


class PreparedArtifact(DTO):
    version: Literal["prepared-v1"] = "prepared-v1"
    document: PreparedDocument | PreparedPdf = Field(repr=False)


class Checkpoint(DTO):
    version: Literal["checkpoint-v1"] = "checkpoint-v1"
    key: ContentHash
    payload_hash: ContentHash
    payload: str = Field(repr=False)


_RESOLUTION_CONFLICTS = frozenset(
    {
        "candidate_changed",
        "character_conflict",
        "relation_direction_conflict",
        "embedding_space_changed",
    }
)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(config: dict[str, object]) -> str:
    return _hash(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())


class _Providers:
    """One attempt-wide budget, including extraction, identity and embeddings."""

    def __init__(self, pipeline: RevisionPipeline, attempt: Attempt) -> None:
        self.pipeline = pipeline
        self.attempt = attempt
        self.calls = 0
        self.input_bytes = 0
        self.stage = Stage.EXTRACTION

    async def reserve(self, size: int) -> None:
        await asyncio.to_thread(self.pipeline.checkpoints.check, self.attempt)
        limits = self.pipeline.limits
        if (
            self.calls >= limits.llm_calls
            or size > limits.llm_input_bytes
            or self.input_bytes + size > limits.llm_total_input_bytes
        ):
            raise PipelineError(ErrorCode.LIMIT_EXCEEDED, self.stage)
        # No await between checking and consuming the budget, even with workers.
        self.calls += 1
        self.input_bytes += size

    async def request(
        self, request: GenerationRequest, response_model: type[BaseModel]
    ) -> None:
        # Count the JSON instruction and schema appended by ChatCompletionsLLM,
        # in addition to caller text and base64 images. Transport framing is not
        # a model-input budget; adapter response-size limits remain independent.
        instruction = (
            files("article_graph").joinpath("prompts/json.v1.txt").read_bytes()
        )
        size = len(request.system.encode()) + len(instruction) + 2
        size += len(json.dumps(response_model.model_json_schema()).encode())
        for block in request.content:
            size += (
                4 * ((len(block.data) + 2) // 3)
                if isinstance(block, ImageBlock)
                else len(block.text.encode())
            )
        if any(isinstance(b, ImageBlock) for b in request.content):
            if not self.pipeline.llm_settings.vision_model:
                raise PipelineError(ErrorCode.EXTRACTION_INVALID, self.stage)
        await self.reserve(size)

    async def generate_with_metadata[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> GenerationResult[T]:
        await self.request(request, response_model)
        async with asyncio.timeout(self.pipeline.limits.provider_timeout_seconds):
            return await self.pipeline.llm.generate_with_metadata(
                request, response_model
            )

    async def generate[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> T:
        await self.request(request, response_model)
        async with asyncio.timeout(self.pipeline.limits.provider_timeout_seconds):
            return await self.pipeline.llm.generate(request, response_model)

    async def extract(self, content: Sequence[TextBlock | ImageBlock]) -> Extraction:
        prompt = files("article_graph").joinpath("prompts/extraction.v1.txt")
        return await self.generate(
            GenerationRequest(
                system=prompt.read_text(encoding="utf-8"), content=tuple(content)
            ),
            Extraction,
        )

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        await self.reserve(sum(len(text.encode()) for text in texts))
        async with asyncio.timeout(self.pipeline.limits.provider_timeout_seconds):
            batch = await self.pipeline.embeddings.embed(texts)
        if batch.metadata != self.pipeline.embedding_metadata:
            raise PipelineError(ErrorCode.EXTRACTION_INVALID, self.stage)
        return batch


class RevisionPipeline:
    def __init__(
        self,
        *,
        checkpoints: CheckpointRepository,
        publisher: GraphPublisher,
        store: BlobStore,
        llm: PipelineLLM,
        embeddings: Embeddings,
        candidates: CandidateStore,
        llm_settings: LLMSettings,
        embedding_settings: OllamaSettings,
        limits: Limits,
        versions: Versions | None = None,
        top_k: int = 10,
        registered_types: tuple[RelationType, ...] = (),
    ) -> None:
        if not 1 <= top_k <= 100:
            raise ValueError("invalid top_k")
        self.checkpoints = checkpoints
        self.publisher = publisher
        self.store = store
        self.llm = llm
        self.embeddings = embeddings
        self.candidates = candidates
        self.llm_settings = llm_settings
        self.embedding_settings = embedding_settings
        self.embedding_metadata = EmbeddingMetadata(
            model=embedding_settings.model,
            digest=embedding_settings.digest,
            dimensions=embedding_settings.dimensions,
        )
        self.limits = limits
        self.versions = versions or Versions()
        self.top_k = top_k
        self.registered_types = registered_types

    def _source(self, source: RevisionInput) -> bytes:
        if source.source_hash != source.content_hash:
            raise DocumentError(ErrorCode.INVALID_DOCUMENT)
        with self.store.open(BlobNamespace.SOURCES, source.source_hash) as stream:
            data = stream.read(self.limits.upload_bytes + 1)
        if len(data) > self.limits.upload_bytes:
            raise DocumentError(ErrorCode.PAYLOAD_TOO_LARGE)
        if len(data) != source.source_size or _hash(data) != source.content_hash:
            raise DocumentError(ErrorCode.INVALID_DOCUMENT)
        return data

    def _load[T: BaseModel](
        self, source: RevisionInput, stage: Stage, key: str, model: type[T]
    ) -> T | None:
        pointer = source.checkpoints.get(stage)
        if pointer is None or pointer.key != key:
            return None
        try:
            with self.store.open(BlobNamespace.ARTIFACTS, pointer.blob_hash) as stream:
                # Bounded independently of the storage implementation/metadata.
                data = stream.read(self.limits.llm_total_input_bytes * 4 + 1)
            if _hash(data) != pointer.blob_hash:
                return None
            saved = Checkpoint.model_validate_json(data)
            if saved.key != key or _hash(saved.payload.encode()) != saved.payload_hash:
                return None
            result = model.model_validate_json(saved.payload)
            if isinstance(result, PreparedArtifact):
                document = result.document
                prepared = (
                    document.prepared if isinstance(document, PreparedPdf) else document
                )
                if prepared.revision_id != source.revision_id:
                    return None
            elif isinstance(result, ExtractionArtifact):
                if (
                    result.revision_id != source.revision_id
                    or result.version != "extraction-v1"
                    or result.prompt_version != "document-v1"
                ):
                    return None
                result.extraction.check_limits(self.limits)
            elif isinstance(result, ResolutionArtifact):
                if result.revision_id != source.revision_id:
                    return None
            # PDF images must still exist and match their immutable addresses.
            if isinstance(result, PreparedArtifact) and isinstance(
                result.document, PreparedPdf
            ):
                for page in result.document.pages:
                    if (
                        page.image.namespace != BlobNamespace.ARTIFACTS
                        or page.image.size > self.limits.page_pixels * 4 + 1_000_000
                    ):
                        return None
                    with self.store.open(
                        page.image.namespace, page.image.content_hash
                    ) as image:
                        data = image.read(page.image.size + 1)
                    if (
                        len(data) != page.image.size
                        or _hash(data) != page.image.content_hash
                    ):
                        return None
            return result
        except FileNotFoundError, ValidationError, ValueError:
            return None

    def _save(
        self, attempt: Attempt, stage: Stage, key: str, payload: BaseModel
    ) -> None:
        serialized = payload.model_dump_json()
        envelope = Checkpoint(
            key=key, payload_hash=_hash(serialized.encode()), payload=serialized
        )
        blob = self.store.put_artifact(BytesIO(envelope.model_dump_json().encode()))
        self.checkpoints.save(
            attempt, stage, Pointer(key=key, blob_hash=blob.content_hash)
        )

    async def run(self, attempt: Attempt) -> PublicationResult:
        providers = _Providers(self, attempt)
        try:
            async with asyncio.timeout(self.limits.processing_seconds):
                return await self._run(attempt, providers)
        except PipelineStopped as stopped:
            return stopped.result
        except PipelineError:
            raise
        except (LLMError, EmbeddingError) as error:
            code = (
                ErrorCode.PROVIDER_UNAVAILABLE
                if error.retryable
                else ErrorCode.EXTRACTION_INVALID
            )
            if error.code == "limit_exceeded":
                code = ErrorCode.LIMIT_EXCEEDED
            elif error.code == "processing_timeout":
                code = ErrorCode.PROCESSING_TIMEOUT
            raise PipelineError(
                code, providers.stage, retryable=error.retryable
            ) from None
        except DocumentError as error:
            raise PipelineError(error.code, providers.stage) from None
        except TimeoutError:
            raise PipelineError(
                ErrorCode.PROCESSING_TIMEOUT, providers.stage, retryable=True
            ) from None
        except ServiceUnavailable, SessionExpired:
            raise PipelineError(
                ErrorCode.SERVICE_UNAVAILABLE, providers.stage, retryable=True
            ) from None
        except Neo4jError as error:
            raise PipelineError(
                ErrorCode.SERVICE_UNAVAILABLE,
                providers.stage,
                retryable=error.is_retryable(),
            ) from None
        except PublicationError as error:
            raise PipelineError(
                ErrorCode.EXTRACTION_INVALID,
                Stage.PUBLICATION,
                retryable=str(error) in _RESOLUTION_CONFLICTS,
            ) from None
        except ValueError:
            raise PipelineError(ErrorCode.EXTRACTION_INVALID, providers.stage) from None
        except OSError:
            raise PipelineError(
                ErrorCode.INTERNAL_ERROR, providers.stage, retryable=True
            ) from None
        except Exception:
            raise PipelineError(ErrorCode.INTERNAL_ERROR, providers.stage) from None

    async def _run(self, attempt: Attempt, providers: _Providers) -> PublicationResult:
        providers.stage = Stage.PREPARATION
        source = await asyncio.to_thread(self.checkpoints.check, attempt)
        data = await asyncio.to_thread(self._source, source)
        common: dict[str, object] = {
            "pipeline": self.versions.pipeline,
            "revision": str(attempt.revision_id),
            "article": str(attempt.article_id),
            "source": source.content_hash,
            "format": source.format.value,
            "limits": self.limits.model_dump(),
        }
        prep_key = _key({**common, "preparation": self.versions.preparation})
        prepared = await asyncio.to_thread(
            self._load, source, Stage.PREPARATION, prep_key, PreparedArtifact
        )
        if prepared is None:
            if source.format == DocumentFormat.PDF:
                with TemporaryDirectory(prefix="pipeline-source-") as directory:
                    path = Path(directory) / "source.pdf"
                    await asyncio.to_thread(path.write_bytes, data)
                    document = await prepare_pdf(
                        path,
                        revision_id=attempt.revision_id,
                        limits=self.limits,
                        store=self.store,
                    )
            else:
                document = await asyncio.to_thread(
                    prepare_markdown,
                    data.decode("utf-8"),
                    revision_id=attempt.revision_id,
                    limits=self.limits,
                )
            prepared = PreparedArtifact(document=document)
            await asyncio.to_thread(
                self._save, attempt, Stage.PREPARATION, prep_key, prepared
            )
        llm_config = self.llm_settings.model_dump(mode="json", exclude={"api_key"})
        prompts = {
            name: _hash(files("article_graph").joinpath(f"prompts/{name}").read_bytes())
            for name in (
                "document-system.v1.txt",
                "document-user.v1.txt",
                "identity.v1.txt",
                "json.v1.txt",
            )
        }
        extraction_key = _key(
            {
                **common,
                "preparation_key": prep_key,
                "preparation": _hash(prepared.model_dump_json().encode()),
                "extraction": self.versions.extraction,
                "llm": llm_config,
                "schema": Extraction.model_json_schema(),
                "prompts": {k: v for k, v in prompts.items() if k != "identity.v1.txt"},
            }
        )
        providers.stage = Stage.EXTRACTION
        extracted = await asyncio.to_thread(
            self._load, source, Stage.EXTRACTION, extraction_key, ExtractionArtifact
        )
        if extracted is None:
            extracted, _ = await extract_document(
                prepared.document,
                provider=providers,
                store=self.store,
                limits=self.limits,
            )
            await asyncio.to_thread(
                self._save, attempt, Stage.EXTRACTION, extraction_key, extracted
            )
        resolution_key = _key(
            {
                **common,
                "extraction_key": extraction_key,
                "extraction": _hash(extracted.model_dump_json().encode()),
                "version": self.versions.resolution,
                "llm": llm_config,
                "prompt": prompts["identity.v1.txt"],
                "json_prompt": prompts["json.v1.txt"],
                "schema": IdentityVerdict.model_json_schema(),
                "embedding": self.embedding_settings.model_dump(mode="json"),
                "embedding_metadata": self.embedding_metadata.model_dump(mode="json"),
                "top_k": self.top_k,
                "types": [t.model_dump(mode="json") for t in self.registered_types],
            }
        )
        providers.stage = Stage.RESOLUTION
        resolved = await asyncio.to_thread(
            self._load, source, Stage.RESOLUTION, resolution_key, ResolutionArtifact
        )
        if resolved is None:
            await asyncio.to_thread(self.checkpoints.check, attempt)
            resolved, _ = await resolve(
                extracted,
                article_id=attempt.article_id,
                embeddings=providers,
                repository=self.candidates,
                llm=providers,
                store=self.store,
                limits=self.limits,
                top_k=self.top_k,
                registered_types=self.registered_types,
            )
            await asyncio.to_thread(
                self._save, attempt, Stage.RESOLUTION, resolution_key, resolved
            )
        providers.stage = Stage.PUBLICATION
        await asyncio.to_thread(self.checkpoints.check, attempt)
        try:
            return await asyncio.to_thread(
                self.publisher.publish,
                Publication(
                    article_id=attempt.article_id,
                    job_id=attempt.job_id,
                    attempt=attempt.attempt,
                    attempt_token=attempt.attempt_token,
                    artifact=resolved,
                ),
            )
        except PublicationError as error:
            if str(error) in _RESOLUTION_CONFLICTS:
                # Refresh graph snapshots on retry; extraction remains reusable.
                await asyncio.to_thread(
                    self.checkpoints.save, attempt, Stage.RESOLUTION, None
                )
            raise
