import os
import sys
from pathlib import Path
lazy from subprocess import run

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from article_graph.app import create_app
from article_graph.settings import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_"):
            monkeypatch.delenv(name)


def test_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", "test-token-" * 4)
    with TestClient(create_app()) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.get("/openapi.json").status_code == 200


def test_required_settings_fail_before_serving() -> None:
    with pytest.raises(ValidationError) as error:
        create_app()
    assert error.value.errors()[0]["loc"] == ("app_token",)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("value", ["", "too-short"])
def test_invalid_token_is_redacted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", value)
    with pytest.raises(ValidationError) as error:
        create_app()
    assert error.value.errors()[0]["loc"] == ("app_token",)
    assert "input_value" not in str(error.value)


def test_dotenv_and_explicit_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text(
        "ARTICLE_GRAPH_APP_TOKEN=" + "file-token-" * 4 + "\n", encoding="utf-8"
    )
    settings = Settings()
    assert settings.app_token.get_secret_value() == "file-token-" * 4
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", "env-token-" * 4)
    assert Settings().app_token.get_secret_value() == "env-token-" * 4
    assert create_app(settings).state.settings is settings


def test_lazy_import_is_deferred_then_executed() -> None:
    # A clean process is essential: pytest/plugins may already import uvicorn.
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "lazy from fractions import Fraction\n"
            "assert 'fractions' not in sys.modules\n"
            "assert Fraction(1, 2) + Fraction(1, 2) == 1\n"
            "assert 'fractions' in sys.modules\n"
            "import article_graph.__main__ as cli\n"
            "assert 'uvicorn' not in sys.modules\n"
            "assert cli.uvicorn.__name__ == 'uvicorn'\n"
            "assert 'uvicorn' in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
