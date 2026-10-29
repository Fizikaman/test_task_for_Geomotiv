"""A deliberately limited, non-streaming Chat Completions adapter.

Each call makes exactly one attempt. The worker owns the bounded retry budget.
"""

import base64
import json
from collections.abc import Sequence
from importlib.resources import files
from typing import Annotated, Literal, Protocol

from fastapi import status as http_status
from pydantic import BaseModel, Field, ValidationError
lazy import httpx

from article_graph.domain import DTO, Extraction, Text
from article_graph.settings import Limits, LLMSettings


class TextBlock(DTO):
    type: Literal["text"] = "text"
    text: Text


class ImageBlock(DTO):
    type: Literal["image"] = "image"
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: bytes = Field(min_length=1, repr=False)


ContentBlock = Annotated[TextBlock | ImageBlock, Field(discriminator="type")]


class GenerationRequest(DTO):
    system: Text
    content: tuple[ContentBlock, ...] = Field(min_length=1)


class LLM(Protocol):
    async def generate[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> T: ...

    async def extract(self, content: Sequence[ContentBlock]) -> Extraction: ...


class LLMError(Exception):
    """Safe fixed code; never retain HTTP exceptions or provider content."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _Message(BaseModel):
    content: str | None = None
    refusal: str | None = None
    role: Literal["assistant"]


class _Choice(BaseModel):
    finish_reason: str
    message: _Message


class _Completion(BaseModel):
    choices: list[_Choice] = Field(min_length=1, max_length=1)


class ChatCompletionsLLM:
    def __init__(
        self,
        settings: LLMSettings,
        limits: Limits,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Required validation is eager, before client creation or any request.
        if not settings.api_key.get_secret_value().strip():
            raise LLMError("llm_key_required")
        self._settings = settings
        self._limits = limits
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=min(settings.timeout_seconds, limits.provider_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract(self, content: Sequence[ContentBlock]) -> Extraction:
        prompt = files("article_graph").joinpath("prompts/extraction.v1.txt")
        result = await self.generate(
            GenerationRequest(
                system=prompt.read_text(encoding="utf-8"), content=tuple(content)
            ),
            Extraction,
        )
        over_limit = False
        try:
            result.check_limits(self._limits)
        except ValueError:
            over_limit = True
        if over_limit:
            raise LLMError("limit_exceeded")
        return result

    async def generate[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> T:
        settings = self._settings
        has_images = any(isinstance(block, ImageBlock) for block in request.content)
        model = settings.vision_model if has_images else settings.text_model
        if model is None:
            raise LLMError("vision_model_required")
        schema = response_model.model_json_schema(mode="validation")
        instruction = (
            files("article_graph")
            .joinpath("prompts/json.v1.txt")
            .read_text(encoding="utf-8")
        )
        blocks: list[dict[str, object]] = []
        for block in request.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            else:
                encoded = base64.b64encode(block.data).decode("ascii")
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{block.media_type};base64,{encoded}"
                        },
                    }
                )
        output: dict[str, object] = {"type": settings.output_mode}
        if settings.output_mode == "json_schema":
            output["json_schema"] = {"name": "result", "schema": schema}
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": request.system
                    + "\n"
                    + instruction
                    + "\n"
                    + json.dumps(schema),
                },
                {"role": "user", "content": blocks},
            ],
            "response_format": output,
            "stream": False,
            "max_tokens": settings.max_tokens,
        }
        failure: LLMError | None = None
        raw = bytearray()
        try:
            async with self._client.stream(
                "POST",
                settings.base_url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": "Bearer " + settings.api_key.get_secret_value()
                },
                json=payload,
            ) as response:
                status = response.status_code
                if status != http_status.HTTP_200_OK:
                    failure = LLMError(
                        "provider_http_error",
                        retryable=status == http_status.HTTP_429_TOO_MANY_REQUESTS
                        or httpx.codes.is_server_error(status),
                    )
                else:
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > settings.max_response_bytes:
                            failure = LLMError("provider_response_too_large")
                            break
        except httpx.TimeoutException:
            failure = LLMError("provider_timeout", retryable=True)
        except httpx.TransportError:
            failure = LLMError("provider_transport_error", retryable=True)
        # Raise outside except: HTTP exception context can contain credentials.
        if failure is not None:
            raise failure
        completion: _Completion | None = None
        try:
            completion = _Completion.model_validate_json(raw, strict=True)
        except ValidationError:
            pass
        if completion is None:
            raise LLMError("provider_response_invalid")
        choice = completion.choices[0]
        if choice.message.refusal or choice.finish_reason == "content_filter":
            raise LLMError("provider_refusal")
        if choice.finish_reason == "length":
            raise LLMError("provider_truncated")
        if choice.finish_reason != "stop":
            raise LLMError("provider_incomplete")
        content = choice.message.content
        if content is None or not content.strip():
            raise LLMError("provider_empty_content")
        result: T | None = None
        try:
            result = response_model.model_validate_json(
                content, strict=True, extra="forbid"
            )
        except ValidationError:
            pass
        if result is None:
            raise LLMError("extraction_invalid")
        return result
