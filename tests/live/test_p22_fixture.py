"""Offline checks for P22 inputs, quality oracle and default/CI opt-out."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from article_graph.documents.blob_store import LocalBlobStore
from article_graph.documents.pdf_preparation import prepare_pdf
from article_graph.domain import Character, Relation, TextEvidence, VisualEvidence
from article_graph.settings import Limits
from tests.live.p22_fixture import Sample, check_graph, samples


def graph(sample: Sample) -> tuple[tuple[Character, ...], tuple[Relation, ...]]:
    characters = (
        Character(
            character_id=UUID(int=1),
            name="Alice",
            aliases=("Ally",),
            description="Synthetic parent.",
        ),
        Character(character_id=UUID(int=2), name="Bob", description="Synthetic child."),
    )
    evidence = (
        VisualEvidence(
            fragment_id="page",
            page=sample.visual_page,
            description="Arrow from Alice to Bob.",
        )
        if sample.visual_page
        else TextEvidence(
            fragment_id="text", quote="Alice, also known as Ally, is Bob's parent."
        )
    )
    relations = (
        Relation(
            relation_id=UUID(int=4),
            article_id=UUID(int=5),
            revision_id=UUID(int=6),
            kind="PARENT_OF",
            directed=True,
            subject_id=UUID(int=1),
            object_id=UUID(int=2),
            evidence=(evidence,),
        ),
    )
    if sample.friendship:
        characters += (
            Character(
                character_id=UUID(int=3), name="Carol", description="Synthetic friend."
            ),
        )
        relations += (
            Relation(
                relation_id=UUID(int=7),
                article_id=UUID(int=5),
                revision_id=UUID(int=6),
                kind="FRIEND_OF",
                directed=False,
                subject_id=UUID(int=3),
                object_id=UUID(int=1),
                evidence=(
                    TextEvidence(
                        fragment_id="text",
                        page=1 if sample.visual_page else None,
                        quote="Alice and Carol are friends.",
                    ),
                ),
            ),
        )
    return characters, relations


@pytest.mark.parametrize("sample", samples(), ids=lambda sample: sample.name)
def test_semantic_oracle_accepts_valid_graph(sample: Sample) -> None:
    characters, relations = graph(sample)
    check_graph(sample, characters, relations)
    # Identity stays stable if the canonical alias/name choice or prose changes.
    alternate = characters[0].model_copy(
        update={
            "name": "Ally",
            "aliases": ("Alice",),
            "description": "Different wording.",
        }
    )
    check_graph(sample, (alternate, *characters[1:]), relations)


@pytest.mark.parametrize("defect", ["direction", "alias", "kind", "evidence", "extra"])
def test_semantic_oracle_rejects_bad_graph(defect: str) -> None:
    sample = samples()[2]
    characters, relations = graph(sample)
    parent, friend = relations
    if defect == "direction":
        parent = parent.model_copy(
            update={"subject_id": UUID(int=2), "object_id": UUID(int=1)}
        )
    elif defect == "alias":
        characters = (characters[0].model_copy(update={"aliases": ()}), *characters[1:])
    elif defect == "kind":
        parent = parent.model_copy(update={"kind": "KNOWS"})
    elif defect == "evidence":
        parent = parent.model_copy(
            update={
                "evidence": (
                    VisualEvidence(
                        fragment_id="wrong-page", page=1, description="Arrow."
                    ),
                )
            }
        )
    elif defect == "extra":
        relations += (parent.model_copy(update={"relation_id": UUID(int=8)}),)
    with pytest.raises(AssertionError):
        check_graph(sample, characters, (parent, friend, *relations[2:]))


@pytest.mark.parametrize("sample", samples()[1:], ids=lambda sample: sample.name)
def test_real_renderer_accepts_synthetic_pdfs(sample: Sample, tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(sample.data)
    store = LocalBlobStore(tmp_path / "blobs", max_bytes=64_000_000)
    document = asyncio.run(
        prepare_pdf(
            source,
            revision_id=UUID(int=1),
            store=store,
            limits=Limits(),
        )
    )
    assert len(document.pages) == sample.visual_page
    diagram = document.pages[-1]
    assert "Alice" in diagram.text and "Ally" in diagram.text and "Bob" in diagram.text
    assert "Alice is Bob" not in diagram.text  # relationship exists only in geometry
    if sample.friendship:
        assert "Alice and Carol are friends." in document.pages[0].text
    for page in document.pages:
        with store.open(page.image.namespace, page.image.content_hash) as stream:
            assert stream.read(8) == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("mode", ["default", "ci", "consent-missing"])
def test_live_gates_do_not_import_provider_or_read_key(mode: str) -> None:
    script = (
        "import sys, pytest; "
        "code = pytest.main(sys.argv[1:]); "
        "assert 'tests.live.p22_live' not in sys.modules; "
        "raise SystemExit(code)"
    )
    arguments = ["tests/live/test_live_provider.py", "-q"]
    environment = {**os.environ, "PYTHONPATH": "src"}
    environment.pop("P22_LIVE_CONSENT", None)
    environment.pop("CI", None)
    if mode != "default":
        arguments.append("--live-provider")
    if mode == "ci":
        environment["CI"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if mode == "consent-missing":
        assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
        assert "3 failed" in result.stdout
        assert "explicit separate-key consent" in result.stdout
    else:
        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        assert "3 skipped" in result.stdout
