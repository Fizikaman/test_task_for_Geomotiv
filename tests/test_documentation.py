"""P23: file links and public README contract against eagerly generated OpenAPI."""

import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import status as http_status
from pydantic import SecretStr

from article_graph.app import create_app
from article_graph.settings import LLMSettings, Settings

ROOT = Path(__file__).resolve().parents[1]


def test_p23_local_links_resolve() -> None:
    for path in (
        ROOT / "README.md",
        ROOT / "docs/decisions.md",
        ROOT / "docs/p23-validation.md",
    ):
        text = path.read_text(encoding="utf-8-sig")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            if target.startswith(("https://", "http://", "#")):
                continue
            destination = (path.parent / unquote(target.split("#", 1)[0])).resolve()
            assert destination.is_relative_to(ROOT), target
            assert destination.is_file(), f"{path.name}: {target}"


def test_readme_route_table_matches_openapi() -> None:
    settings = Settings(
        _env_file=None,
        app_token=SecretStr("documentation-app-token-32-characters"),
        jwt_access_key=SecretStr("documentation-access-key-32-characters"),
        jwt_refresh_key=SecretStr("documentation-refresh-key-32-characters"),
        llm=LLMSettings(
            base_url="https://provider.test/v1",
            api_key=SecretStr("test-only"),
            text_model="test-text",
            output_mode="json_object",
        ),
    )
    schema = create_app(settings).openapi()
    readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    table = re.findall(r"\| (GET|POST) \| `([^`]+)` \| ([^|]+) \| ([^|]+) \|", readme)
    documented = {(method.lower(), path) for method, path, _, _ in table}
    actual = {
        (method, path)
        for path, methods in schema["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert documented == actual
    for method, path, access, responses in table:
        operation = schema["paths"][path][method.lower()]
        assert bool(operation.get("security")) == (access.strip() == "Access")
        success = re.search(r"\d+", responses)
        assert success is not None
        assert success.group() in operation["responses"]
    upload = schema["paths"]["/articles/upload"]["post"]
    assert str(http_status.HTTP_202_ACCEPTED) in upload["responses"]
    assert str(http_status.HTTP_200_OK) in upload["responses"]
    body = upload["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert set(body["properties"]) == {"text", "file", "article_id"}
    assert body["oneOf"] == [{"required": ["text"]}, {"required": ["file"]}]
    for path in (
        "/characters",
        "/characters/by_article/{article_id}",
        "/relations/types",
        "/relations/connected/{character_id}",
    ):
        parameters = schema["paths"][path]["get"]["parameters"]
        assert {"limit", "offset"} <= {parameter["name"] for parameter in parameters}
    connected = schema["paths"]["/relations/connected/{character_id}"]["get"]
    assert "type" in {parameter["name"] for parameter in connected["parameters"]}


def test_readme_provider_keys_are_placeholders() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    values = re.findall(
        r"^ARTICLE_GRAPH_(?:LLM__API_KEY|APP_TOKEN|JWT_ACCESS_KEY|JWT_REFRESH_KEY|NEO4J__PASSWORD)=(.+)$",
        text,
        re.MULTILINE,
    )
    assert values
    assert all(value.startswith("<") and value.endswith(">") for value in values)
