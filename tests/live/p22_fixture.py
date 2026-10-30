"""Explicitly synthetic P22 inputs and semantic oracle; no employer material."""

from dataclasses import dataclass
from typing import Literal

from article_graph.domain import Character, Relation


@dataclass(frozen=True)
class Sample:
    name: str
    data: bytes
    content_type: Literal["text/markdown", "application/pdf"]
    visual_page: int | None
    friendship: bool


def _pdf(streams: tuple[bytes, ...]) -> bytes:
    pages = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(streams)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {len(streams)} /Kids [{pages}] >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, stream in enumerate(streams):
        objects.extend(
            [
                f"<< /Length {len(stream)} >>\nstream\n".encode()
                + stream
                + b"\nendstream",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] "
                b"/Resources << /Font << /F1 3 0 R >> >> "
                + f"/Contents {4 + 2 * i} 0 R >>".encode(),
            ]
        )
    data = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{start}\n%%EOF".encode()
    )
    return bytes(data)


def samples() -> tuple[Sample, ...]:
    diagram = (
        b"BT /F1 16 Tf 30 350 Td (Synthetic family diagram) Tj "
        b"0 -30 Td (Legend: arrow points from parent to child.) Tj "
        b"0 -80 Td (Alice, also known as Ally) Tj 390 0 Td (Bob) Tj ET "
        b"2 w 140 180 m 450 180 l S 435 190 m 450 180 l 435 170 l S"
    )
    text = (
        b"BT /F1 16 Tf 30 350 Td (Synthetic notes) Tj "
        b"0 -40 Td (Alice is also known as Ally.) Tj "
        b"0 -40 Td (Alice and Carol are friends.) Tj ET"
    )
    markdown = (
        b"# Synthetic family\nAlice, also known as Ally, is Bob's parent.\n"
        b"Alice and Carol are friends. Bob is not Alice's parent.\n"
    )
    return (
        Sample("markdown", markdown, "text/markdown", None, True),
        Sample("visual-diagram", _pdf((diagram,)), "application/pdf", 1, False),
        Sample("mixed-pdf", _pdf((text, diagram)), "application/pdf", 2, True),
    )


def check_graph(
    sample: Sample, characters: tuple[Character, ...], relations: tuple[Relation, ...]
) -> None:
    """Accept prose/ID variation, reject loss of meaning, aliases or provenance."""

    def person(name: str) -> Character:
        matches = [
            c
            for c in characters
            if name.casefold() in {s.casefold() for s in (c.name, *c.aliases)}
        ]
        assert len(matches) == 1, "missing or duplicated synthetic person"
        return matches[0]

    alice, bob = person("Alice"), person("Bob")
    assert person("Ally").character_id == alice.character_id, "alias split/lost"
    assert len(characters) == (3 if sample.friendship else 2), "extra/missing people"
    parent = [r for r in relations if r.kind == "PARENT_OF"]
    assert len(parent) == 1, "missing or extra parent relation"
    assert parent[0].directed
    assert (parent[0].subject_id, parent[0].object_id) == (
        alice.character_id,
        bob.character_id,
    ), "reversed parent direction"
    if sample.visual_page is not None:
        assert any(
            e.kind == "visual" and e.page == sample.visual_page
            for e in parent[0].evidence
        ), "visual relation needs visual evidence on the diagram page"
    else:
        assert all(e.kind == "text" and e.page is None for e in parent[0].evidence)
    if sample.friendship:
        carol = person("Carol")
        friends = [r for r in relations if r.kind == "FRIEND_OF"]
        assert len(friends) == 1 and not friends[0].directed
        assert {friends[0].subject_id, friends[0].object_id} == {
            alice.character_id,
            carol.character_id,
        }
        assert any(
            e.kind == "text" and e.page == (1 if sample.visual_page else None)
            for e in friends[0].evidence
        ), "friendship needs evidence from the text page"
    assert len(relations) == (2 if sample.friendship else 1), "unsupported assertions"
    for relation in relations:
        for evidence in relation.evidence:
            assert evidence.fragment_id
            if evidence.kind == "text":
                if sample.content_type == "text/markdown":
                    assert evidence.quote in sample.data.decode()
                else:
                    assert evidence.page in {1, 2}
