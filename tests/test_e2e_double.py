"""Verify the HTTP double fails closed without actual rendered diagram bytes."""

import base64
import json
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import pytest
from fastapi import status as http_status

from article_graph.pdf_preparation import PdfManifest
from article_graph.pdf_worker import process_pdf
from article_graph.settings import Limits
from tests.p21_doubles import E2EHandler
from tests.p21_fixture import diagram_pdf


@pytest.fixture
def double() -> Iterator[httpx.Client]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), E2EHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}",
            timeout=5,
            trust_env=False,
        ) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("image", ["missing", "invalid", "truncated", "blank", "arrow"])
def test_vision_double_checks_received_pixels(
    double: httpx.Client,
    tmp_path: Path,
    image: str,
) -> None:
    envelope = {
        "fragment_id": "page-1",
        "page": 1,
        "image_fragment_id": "page-1",
        "text": "Alice Bob",
    }
    blocks: list[dict[str, object]] = [{"type": "text", "text": json.dumps(envelope)}]
    if image != "missing":
        data = b"not a PNG"
        if image in {"blank", "arrow", "truncated"}:
            path = tmp_path / "diagram.pdf"
            path.write_bytes(diagram_pdf(arrow=image != "blank"))
            process_pdf(path, tmp_path, uuid4(), Limits())
            manifest = PdfManifest.model_validate_json(
                (tmp_path / "manifest.json").read_text(encoding="utf-8")
            )
            assert manifest.prepared.sources[0].text.split() == ["Alice", "Bob"]
            data = (tmp_path / "1.png").read_bytes()
            if image == "truncated":
                data = data[:30]
        blocks.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(data).decode(),
                },
            }
        )
    before = double.get("/control").json()
    response = double.post(
        "/v1/chat/completions",
        json={
            "model": "p21-vision",
            "messages": [
                {"role": "system", "content": "Extract"},
                {"role": "user", "content": blocks},
            ],
        },
    )
    expected = (
        http_status.HTTP_200_OK
        if image == "arrow"
        else http_status.HTTP_400_BAD_REQUEST
    )
    assert response.status_code == expected
    after = double.get("/control").json()
    assert after["images"] - before["images"] == int(image == "arrow")
    assert after["rejected_images"] - before["rejected_images"] == int(image != "arrow")
