"""P21 HTTP providers. Vision succeeds only for the received diagram pixels."""

import base64
import json
import struct
import zlib
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import override

from tests.p20_doubles import DIGEST, MODEL, Handler, counts, lock

counts.update(images=0, rejected_images=0)


def verify_diagram(url: str) -> None:
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    data = base64.b64decode(url[len(prefix) :], validate=True)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    compressed = bytearray()
    header = b""
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert zlib.crc32(kind + body) == checksum
        if kind == b"IHDR":
            header = body
        if kind == b"IDAT":
            compressed.extend(body)
        offset += length + 12
    # Fixture is a 100pt square rendered at 144dpi, RGB, unfiltered scanlines.
    assert struct.unpack(">IIBBBBB", header) == (200, 200, 8, 2, 0, 0, 0)
    pixels = zlib.decompress(compressed)
    assert len(pixels) == 200 * 601
    assert all(pixels[row * 601] == 0 for row in range(200))

    def red(x: int, y: int) -> bool:
        start = y * 601 + 1 + x * 3
        r, g, b = pixels[start : start + 3]
        return r > 200 and g < 50 and b < 50

    # Horizontal shaft plus arrowhead. Blank/unrelated valid PNGs fail as well.
    assert all(red(x, 160) for x in (30, 70, 110, 150, 170))
    assert red(166, 153) and red(166, 167)
    assert not red(100, 100)


class E2EHandler(Handler):
    @override
    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            super().do_POST()
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        blocks = payload["messages"][1]["content"]
        text = blocks[0]["text"]
        if '"fragment_id"' not in text:
            identity = json.loads(text)
            source = identity["source"]
            candidate = next(
                (c for c in identity["candidates"] if c["name"] == source["name"]),
                None,
            )
            result = {
                "decision": "match" if candidate else "no_match",
                "candidate_id": candidate["character_id"] if candidate else None,
                "source_context": source["description"] if candidate else None,
                "candidate_context": candidate["description"] if candidate else None,
                "reason": "Controlled identity evidence.",
            }
        else:
            envelope = json.loads(text.rsplit("\n", 1)[-1])
            visual = envelope["page"] is not None
            if visual:
                try:
                    assert payload["model"] == "p21-vision"
                    assert len(blocks) == 2 and blocks[1]["type"] == "image_url"
                    verify_diagram(blocks[1]["image_url"]["url"])
                except AssertionError, ValueError, KeyError, struct.error, zlib.error:
                    with lock:
                        counts["rejected_images"] += 1
                    self.reply({}, HTTPStatus.BAD_REQUEST)
                    return
                with lock:
                    counts["images"] += 1
            names = (
                ("Alice", "Bob")
                if visual or "P21_CHANGED" not in text
                else ("Alice", "Carol")
            )
            evidence = (
                {
                    "kind": "visual",
                    "fragment_id": envelope["image_fragment_id"],
                    "page": envelope["page"],
                    "description": "Red arrow Alice to Bob.",
                }
                if visual
                else {
                    "kind": "text",
                    "fragment_id": envelope["fragment_id"],
                    "page": None,
                    "quote": f"{names[0]} met {names[1]}.",
                }
            )
            result = {
                "characters": [
                    {
                        "local_id": name,
                        "name": name,
                        "description": "P21 person with stable identity context.",
                    }
                    for name in names
                ],
                "relations": [
                    {
                        "local_id": "link",
                        "subject_id": names[0],
                        "object_id": names[1],
                        "kind": "PARENT_OF" if visual else "FRIEND_OF",
                        "directed": visual,
                        "polarity": "affirmed",
                        "evidence": [evidence],
                    }
                ],
            }
        with lock:
            counts["llm"] += 1
        self.reply(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(result)},
                    }
                ]
            }
        )


if __name__ == "__main__":
    # P20's embedding model/digest are explicitly retained in P21 Compose.
    assert MODEL == "p20-test:v1" and DIGEST == "f" * 64
    ThreadingHTTPServer(("0.0.0.0", 8080), E2EHandler).serve_forever()
