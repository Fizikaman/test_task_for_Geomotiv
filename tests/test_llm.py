import asyncio
import base64
import json
import os
import subprocess
import sys
from collections.abc import Callable

import httpx
import pytest
from fastapi import status as http_status
from pydantic import Field, SecretStr, ValidationError

from article_graph.domain import DTO, Extraction
from article_graph.llm import (
    LLM,
    ChatCompletionsLLM,
    GenerationRequest,
    ImageBlock,
    LLMError,
    TextBlock,
)
from article_graph.settings import Limits, LLMSettings, Settings


class Result(DTO):
    count: int = Field(ge=0)


# Deliberately unregistered code: verify classification of the whole 5xx range.
UNREGISTERED_SERVER_ERROR = 599


def config(**overrides: object) -> LLMSettings:
    return LLMSettings.model_validate(
        {
            "api_key": SecretStr("mock-key-only"),
            "base_url": "https://provider.test/v1/",
            "text_model": "text-test",
            "output_mode": "json_object",
            "vision_model": "vision-test",
            **overrides,
        }
    )


def completion(
    content: str | None = '{"count":1}',
    *,
    finish: str = "stop",
    refusal: str | None = None,
) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                },
            }
        ]
    }


def invoke(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: LLMSettings | None = None,
    request: GenerationRequest | None = None,
) -> Result:
    async def run() -> Result:
        adapter = ChatCompletionsLLM(
            settings or config(), Limits(), transport=httpx.MockTransport(handler)
        )
        provider: LLM = adapter
        try:
            return await provider.generate(
                request
                or GenerationRequest(
                    system="test system", content=(TextBlock(text="source"),)
                ),
                Result,
            )
        finally:
            await adapter.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("mode", ["json_object", "json_schema"])
@pytest.mark.parametrize("image", [False, True])
def test_payload(mode: str, image: bool) -> None:
    image_data = b"\x89PNG\r\nsynthetic-image"
    blocks = (
        (
            TextBlock(text="fragment-1 page 1"),
            ImageBlock(media_type="image/png", data=image_data),
        )
        if image
        else (TextBlock(text="fragment-1 source"),)
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "https://provider.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer mock-key-only"
        assert request.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 30
        )
        payload = json.loads(request.content)
        assert payload["model"] == ("vision-test" if image else "text-test")
        assert payload["stream"] is False
        assert payload["max_tokens"] == 4096
        output = payload["response_format"]
        assert output["type"] == mode
        if mode == "json_schema":
            assert output["json_schema"]["schema"] == Result.model_json_schema()
        assert "JSON" in payload["messages"][0]["content"]
        assert "test system" in payload["messages"][0]["content"]
        content = payload["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": blocks[0].text}
        if image:
            url = content[1]["image_url"]["url"]
            assert url.startswith("data:image/png;base64,")
            assert base64.b64decode(url.split(",", 1)[1]) == image_data
        return httpx.Response(http_status.HTTP_200_OK, json=completion())

    assert invoke(
        handler,
        settings=config(output_mode=mode),
        request=GenerationRequest(system="test system", content=blocks),
    ) == Result(count=1)
    assert calls == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "provider_response_invalid"),
        ({"choices": []}, "provider_response_invalid"),
        (
            {"choices": [{"message": {"role": "assistant", "content": "{}"}}]},
            "provider_response_invalid",
        ),
        (completion(finish="length"), "provider_truncated"),
        (completion(finish="tool_calls"), "provider_incomplete"),
        (completion(finish="content_filter"), "provider_refusal"),
        (completion(refusal="private refusal"), "provider_refusal"),
        (completion(None), "provider_empty_content"),
        (completion("  "), "provider_empty_content"),
        (completion("{"), "extraction_invalid"),
        (completion('```json\n{"count":1}\n```'), "extraction_invalid"),
        (completion('{"count":"1"}'), "extraction_invalid"),
        (completion('{"count":-1}'), "extraction_invalid"),
        (completion('{"count":1,"secret":"private source"}'), "extraction_invalid"),
    ],
)
def test_invalid_response(body: dict[str, object], code: str) -> None:
    with pytest.raises(LLMError) as error:
        invoke(lambda request: httpx.Response(http_status.HTTP_200_OK, json=body))
    assert error.value.code == code
    assert not error.value.retryable
    assert error.value.__context__ is None
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "status",
    [
        http_status.HTTP_301_MOVED_PERMANENTLY,
        http_status.HTTP_400_BAD_REQUEST,
        http_status.HTTP_401_UNAUTHORIZED,
        http_status.HTTP_403_FORBIDDEN,
        http_status.HTTP_404_NOT_FOUND,
        http_status.HTTP_408_REQUEST_TIMEOUT,
        http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        http_status.HTTP_429_TOO_MANY_REQUESTS,
        http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        http_status.HTTP_502_BAD_GATEWAY,
        http_status.HTTP_503_SERVICE_UNAVAILABLE,
        UNREGISTERED_SERVER_ERROR,
    ],
)
def test_http_classification_single_attempt(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            text="private provider body",
            headers={"location": "https://other.test", "retry-after": "1"},
        )

    with pytest.raises(LLMError) as error:
        invoke(handler)
    assert error.value.retryable == (
        status == http_status.HTTP_429_TOO_MANY_REQUESTS
        or status >= http_status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    assert error.value.code == "provider_http_error"
    assert calls == 1
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "kind",
    [
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    ],
)
def test_network_errors(kind: type[httpx.TransportError]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise kind("private key and document", request=request)

    with pytest.raises(LLMError) as error:
        invoke(handler)
    assert error.value.retryable
    assert error.value.__context__ is None
    assert "private" not in str(error.value)
    assert calls == 1


def test_invalid_envelope_and_size_limit() -> None:
    with pytest.raises(LLMError, match="provider_response_invalid"):
        invoke(lambda request: httpx.Response(http_status.HTTP_200_OK, text="not json"))
    with pytest.raises(LLMError, match="provider_response_too_large"):
        invoke(
            lambda request: httpx.Response(http_status.HTTP_200_OK, json=completion()),
            settings=config(max_response_bytes=10),
        )


@pytest.mark.parametrize("key", ["", "   "])
def test_no_key_no_fake(key: str) -> None:
    with pytest.raises(LLMError, match="llm_key_required"):
        ChatCompletionsLLM(
            config().model_copy(update={"api_key": SecretStr(key)}), Limits()
        )


def test_vision_requires_explicit_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("must fail before HTTP")

    with pytest.raises(LLMError, match="vision_model_required"):
        invoke(
            handler,
            settings=config(vision_model=None),
            request=GenerationRequest(
                system="test",
                content=(ImageBlock(media_type="image/png", data=b"bytes"),),
            ),
        )


def test_extract_prompt_and_limits() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert "explicitly affirmed" in payload["messages"][0]["content"]
            return httpx.Response(
                http_status.HTTP_200_OK,
                json=completion('{"characters":[],"relations":[]}'),
            )

        adapter = ChatCompletionsLLM(
            config(), Limits(), transport=httpx.MockTransport(handler)
        )
        try:
            assert await adapter.extract(
                [TextBlock(text="chunk-1 No people.")]
            ) == Extraction(characters=(), relations=())
        finally:
            await adapter.aclose()

    asyncio.run(run())


def test_extraction_rejects_dangling_reference_and_limit() -> None:
    async def run() -> None:
        character = {"local_id": "a", "name": "A", "description": "person"}
        cases: list[tuple[dict[str, object], str]] = [
            (
                {
                    "characters": [character, {**character, "local_id": "b"}],
                    "relations": [],
                },
                "limit_exceeded",
            ),
            (
                {
                    "characters": [character],
                    "relations": [
                        {
                            "local_id": "r",
                            "subject_id": "a",
                            "object_id": "missing",
                            "kind": "FRIEND_OF",
                            "directed": False,
                            "polarity": "affirmed",
                            "evidence": [
                                {
                                    "kind": "text",
                                    "fragment_id": "chunk-1",
                                    "page": None,
                                    "quote": "source",
                                }
                            ],
                        }
                    ],
                },
                "extraction_invalid",
            ),
        ]
        for body, code in cases:

            def handler(
                request: httpx.Request, body: dict[str, object] = body
            ) -> httpx.Response:
                return httpx.Response(
                    http_status.HTTP_200_OK, json=completion(json.dumps(body))
                )

            adapter = ChatCompletionsLLM(
                config(), Limits(characters=1), transport=httpx.MockTransport(handler)
            )
            try:
                with pytest.raises(LLMError, match=code):
                    await adapter.extract([TextBlock(text="chunk-1 source")])
            finally:
                await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "https://key@host",
        "https://host?key=secret",
        "https://host/#fragment",
        "not-url",
    ],
)
def test_bad_endpoint(url: str) -> None:
    with pytest.raises(ValidationError):
        config(base_url=url)


def test_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", "app-" * 10)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_ACCESS_KEY", "access-" * 10)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_REFRESH_KEY", "refresh-" * 10)
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__TEXT_MODEL", "custom-model")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__BASE_URL", "https://custom.test/v1")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__API_KEY", "test-key-only")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__OUTPUT_MODE", "json_schema")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__TIMEOUT_SECONDS", "12")
    settings = Settings(_env_file=None)
    assert settings.llm.text_model == "custom-model"
    assert settings.llm.base_url == "https://custom.test/v1"
    assert settings.llm.output_mode == "json_schema"
    assert settings.llm.timeout_seconds == 12


def test_lazy_http_import_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nimport article_graph.llm as llm\n"
            "assert 'httpx' not in sys.modules\n"
            "from article_graph.settings import LLMSettings, Limits\n"
            "from pydantic import SecretStr\n"
            "import asyncio\n"
            "settings = LLMSettings(base_url='https://provider.test/v1', "
            "api_key=SecretStr('test'), text_model='test-model', output_mode='json_object')\n"
            "client = llm.ChatCompletionsLLM(settings, Limits())\n"
            "assert 'httpx' in sys.modules\n"
            "asyncio.run(client.aclose())\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_nonempty_extraction_and_nested_validation() -> None:
    async def run() -> None:
        body: dict[str, object] = {
            "characters": [
                {"local_id": "a", "name": "A", "description": "parent"},
                {"local_id": "b", "name": "B", "description": "child"},
            ],
            "relations": [
                {
                    "local_id": "r",
                    "subject_id": "a",
                    "object_id": "b",
                    "kind": "PARENT_OF",
                    "directed": True,
                    "polarity": "affirmed",
                    "evidence": [
                        {
                            "kind": "text",
                            "fragment_id": "chunk-1",
                            "page": None,
                            "quote": "A is the parent of B.",
                        }
                    ],
                }
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                http_status.HTTP_200_OK, json=completion(json.dumps(body))
            )

        adapter = ChatCompletionsLLM(
            config(), Limits(), transport=httpx.MockTransport(handler)
        )
        try:
            result = await adapter.extract(
                [TextBlock(text="chunk-1 A is the parent of B.")]
            )
            assert len(result.characters) == 2
            assert result.relations[0].kind == "PARENT_OF"
            assert result.relations[0].directed
            assert result.relations[0].subject_id == "a"
        finally:
            await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(("timeout", "expected"), [(7, 7), (90, 30)])
def test_timeout_bounded_by_limits(timeout: int, expected: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == expected
        return httpx.Response(http_status.HTTP_200_OK, json=completion())

    invoke(handler, settings=config(timeout_seconds=timeout))


@pytest.mark.parametrize("field", ["base_url", "api_key", "text_model", "output_mode"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_llm_settings_fail_before_serving(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str | None
) -> None:
    from article_graph.app import create_app

    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", "app-" * 10)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_ACCESS_KEY", "access-" * 10)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_REFRESH_KEY", "refresh-" * 10)
    values = {
        "base_url": "https://provider.test/v1",
        "api_key": "test-key-only",
        "text_model": "test-model",
        "output_mode": "json_object",
    }
    for name, configured in values.items():
        if name != field:
            monkeypatch.setenv("ARTICLE_GRAPH_LLM__" + name.upper(), configured)
    if value is not None:
        monkeypatch.setenv("ARTICLE_GRAPH_LLM__" + field.upper(), value)
    with pytest.raises(ValidationError) as error:
        create_app(Settings(_env_file=None))
    assert any(item["loc"][:1] == ("llm",) for item in error.value.errors())
    assert "test-key-only" not in str(error.value)


def test_no_llm_configuration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_LLM"):
            monkeypatch.delenv(name)
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            app_token="app-" * 10,
            jwt_access_key="access-" * 10,
            jwt_refresh_key="refresh-" * 10,
        )
    assert any(
        item["loc"] == ("llm",) and item["type"] == "missing"
        for item in error.value.errors()
    )
