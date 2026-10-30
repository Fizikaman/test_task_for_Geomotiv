"""Controlled HTTP providers, served only on the isolated P20 Docker network.

No document bodies or credentials are logged. Control exposes counters and gates.
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock
from typing import override

MODEL = "p20-test:v1"
DIGEST = "f" * 64
lock = Lock()
gate = Event()
gate.set()
counts: dict[str, int] = {"llm": 0, "embed": 0, "blocked": 0, "released": 0}
block_next = False


class Handler(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def reply(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/control":
            with lock:
                self.reply(dict(counts))
        elif self.path == "/api/tags":
            self.reply({"models": [{"name": MODEL, "digest": DIGEST}]})
        else:
            self.reply({}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        global block_next
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/control":
            with lock:
                if payload.get("block_next"):
                    block_next = True
                    gate.clear()
                if payload.get("release"):
                    gate.set()
            self.reply({})
            return
        if self.path == "/api/embed":
            with lock:
                counts["embed"] += 1
            self.reply(
                {
                    "model": MODEL,
                    "embeddings": [[1.0] + [0.0] * 1023 for _ in payload["input"]],
                }
            )
            return
        if self.path != "/v1/chat/completions":
            self.reply({}, HTTPStatus.NOT_FOUND)
            return
        with lock:
            counts["llm"] += 1
            blocked = block_next
            block_next = False
            if blocked:
                counts["blocked"] += 1
        if blocked:
            if not gate.wait(55):
                self.reply({}, HTTPStatus.GATEWAY_TIMEOUT)
                return
            with lock:
                counts["released"] += 1
        text = payload["messages"][1]["content"][0]["text"]
        if "P20_FAIL" in text:
            self.reply({}, HTTPStatus.BAD_REQUEST)
            return
        if '"fragment_id"' in text:
            source = json.loads(text.rsplit("\n", 1)[-1])
            result: object = {
                "characters": [
                    {
                        "local_id": name,
                        "name": name,
                        "description": "P20 person with stable identity context.",
                    }
                    for name in ("Alice", "Bob")
                ],
                "relations": []
                if "P20_EMPTY" in text
                else [
                    {
                        "local_id": "friend",
                        "subject_id": "Alice",
                        "object_id": "Bob",
                        "kind": "FRIEND_OF",
                        "directed": False,
                        "polarity": "affirmed",
                        "evidence": [
                            {
                                "kind": "text",
                                "fragment_id": source["fragment_id"],
                                "page": source["page"],
                                "quote": "Alice met Bob.",
                            }
                        ],
                    }
                ],
            }
        else:
            identity = json.loads(text)
            source = identity["source"]
            candidate = next(
                (
                    item
                    for item in identity["candidates"]
                    if item["name"] == source["name"]
                ),
                None,
            )
            result = {
                "decision": "match" if candidate else "no_match",
                "candidate_id": candidate["character_id"] if candidate else None,
                "source_context": source["description"] if candidate else None,
                "candidate_context": candidate["description"] if candidate else None,
                "reason": "Controlled identity evidence.",
            }
        self.reply(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(result),
                        },
                    }
                ]
            }
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
