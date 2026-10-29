import os
import subprocess
import sys
from uuid import UUID

import pytest
from pydantic import ValidationError

from article_graph.document_format import DocumentError
from article_graph.domain import ErrorCode
from article_graph.preparation import SourceText, prepare_markdown, prepare_sources
from article_graph.settings import Limits

REVISION = UUID(int=1)


def budget(size: int = 64, overlap: int = 8) -> Limits:
    return Limits(chunk_chars=size, chunk_overlap_chars=overlap, chunks=1000)


@pytest.mark.parametrize(
    "text",
    [
        "# Анна и Иван\r\n\r\n**Анна** — мать Ивана. 😀\r\n" * 4,
        "## Alice\n\nAlice is Bob's parent.\n\n" * 4,
        "| Имя | Родитель |\n| --- | --- |\n| Иван | Анна |\n" * 4,
        "![Подпись: Анна](https://invalid.test/image.png)\n"
        "[Ivan](https://invalid.test/) <b>Alice</b>\n" * 4,
        "\ufeff# Имена\n\n" + "e\u0301😀Я" * 80 + "\r\nTAIL",
        "x" * 195,
    ],
)
@pytest.mark.parametrize("overlap", [0, 1, 8, 63])
def test_lossless_spans_and_bounded_overlap(text: str, overlap: int) -> None:
    result = prepare_markdown(
        text, revision_id=REVISION, limits=budget(overlap=overlap)
    )
    assert result.sources == (SourceText("markdown", text),)
    restored = ""
    previous_end = 0
    previous_start = -1
    for chunk in result.chunks:
        assert chunk.text == text[chunk.start : chunk.end]
        assert 0 < len(chunk.text) <= 64
        assert previous_start < chunk.start <= previous_end
        assert previous_end < chunk.end
        assert previous_end - chunk.start <= overlap
        assert chunk.page is None
        restored += chunk.text[previous_end - chunk.start :]
        previous_start, previous_end = chunk.start, chunk.end
    assert restored == text
    assert previous_end == len(text)
    assert len({chunk.fragment_id for chunk in result.chunks}) == len(result.chunks)


def test_paragraph_line_and_word_boundaries() -> None:
    paragraphs = "# Заголовок\n\n" + "А" * 22 + "\n\n" + "B" * 60
    result = prepare_markdown(paragraphs, revision_id=REVISION, limits=budget())
    assert result.chunks[0].text == paragraphs[:37]
    rows = "| Alice | Bob |\n" * 12
    result = prepare_markdown(rows, revision_id=REVISION, limits=budget())
    assert all(chunk.text.endswith("\n") for chunk in result.chunks)
    words = "Alice Bob " * 20
    result = prepare_markdown(words, revision_id=REVISION, limits=budget())
    assert all(chunk.text.endswith(" ") for chunk in result.chunks)


@pytest.mark.parametrize("text", ["", " \t\r\n", "\ufeff \n"])
def test_empty_document(text: str) -> None:
    with pytest.raises(DocumentError) as error:
        prepare_markdown(text, revision_id=REVISION, limits=budget())
    assert error.value.code == ErrorCode.VALIDATION_ERROR


def test_invalid_markdown_has_safe_error() -> None:
    with pytest.raises(DocumentError) as error:
        prepare_markdown("secret\x00", revision_id=REVISION, limits=budget())
    assert str(error.value) == "invalid_document"


def test_budgets_include_overlap_and_never_return_partial_tail() -> None:
    for limits in (
        Limits(chunk_chars=10, chunk_overlap_chars=0, chunks=1),
        Limits(chunk_chars=10, chunk_overlap_chars=0, prepared_chars=19),
        Limits(chunk_chars=10, chunk_overlap_chars=2, prepared_chars=20),
    ):
        with pytest.raises(DocumentError) as error:
            prepare_markdown("x" * 20, revision_id=REVISION, limits=limits)
        assert error.value.code == ErrorCode.LIMIT_EXCEEDED
    exact = Limits(chunk_chars=10, chunk_overlap_chars=2, prepared_chars=24, chunks=3)
    result = prepare_markdown("x" * 20, revision_id=REVISION, limits=exact)
    assert sum(len(chunk.text) for chunk in result.chunks) == 24
    assert result.chunks[-1].end == 20


def test_page_sources_have_local_offsets_and_global_budget() -> None:
    sources = (
        SourceText("page-1", "Alice " * 12, page=1),
        SourceText("page-2", "", page=2),
        SourceText("page-3", "Анна " * 12, page=3),
    )
    result = prepare_sources(sources, revision_id=REVISION, limits=budget())
    assert result.sources == sources
    for source in sources:
        chunks = [
            chunk for chunk in result.chunks if chunk.source_id == source.source_id
        ]
        if source.text:
            assert chunks[0].start == 0
            assert chunks[-1].end == len(source.text)
        for chunk in chunks:
            assert chunk.text == source.text[chunk.start : chunk.end]
            assert chunk.page == source.page
    with pytest.raises(DocumentError):
        prepare_sources(
            sources, revision_id=REVISION, limits=Limits(prepared_chars=100)
        )
    with pytest.raises(DocumentError):
        prepare_sources(sources, revision_id=REVISION, limits=Limits(chunks=1))


def test_stable_ids_across_processes_and_revision_isolation() -> None:
    result = prepare_markdown("# Анна\n", revision_id=REVISION, limits=Limits())
    code = (
        "from uuid import UUID; from article_graph.settings import Limits; "
        "from article_graph.preparation import prepare_markdown; "
        "print(prepare_markdown('# Анна\\n', revision_id=UUID(int=1), "
        "limits=Limits()).chunks[0].fragment_id)"
    )
    for seed in ("1", "42"):
        process = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert process.stdout.strip() == result.chunks[0].fragment_id
    changed = prepare_markdown("# Анна\n", revision_id=UUID(int=2), limits=Limits())
    assert changed.chunks[0].fragment_id != result.chunks[0].fragment_id
    changed = prepare_markdown("# Anna\n", revision_id=REVISION, limits=Limits())
    assert changed.chunks[0].fragment_id != result.chunks[0].fragment_id
    pages = prepare_sources(
        (SourceText("p1", "same", 1), SourceText("p2", "same", 2)),
        revision_id=REVISION,
        limits=Limits(),
    )
    assert pages.chunks[0].fragment_id != pages.chunks[1].fragment_id


@pytest.mark.parametrize("overlap", [-1, 8, 9])
def test_invalid_overlap_settings(overlap: int) -> None:
    with pytest.raises(ValidationError):
        Limits(chunk_chars=8, chunk_overlap_chars=overlap)


def test_source_identity_and_safe_representations() -> None:
    with pytest.raises(ValueError):
        SourceText("", "secret")
    with pytest.raises(ValueError):
        SourceText("page", "secret", 0)
    with pytest.raises(ValueError):
        prepare_sources(
            (SourceText("p", "a"), SourceText("p", "b")),
            revision_id=REVISION,
            limits=Limits(),
        )
    result = prepare_markdown("secret contents", revision_id=REVISION, limits=Limits())
    assert "secret contents" not in repr(result)


def test_preparation_does_not_import_optional_adapters_or_fetch_links() -> None:
    code = """
import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('unexpected network call')
sys.addaudithook(deny_network)
from uuid import UUID
from article_graph.preparation import prepare_markdown
from article_graph.settings import Limits
prepare_markdown('![Alice](https://invalid.test/image)',
                 revision_id=UUID(int=1), limits=Limits())
assert not {'pypdfium2', 'httpx', 'neo4j'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)
