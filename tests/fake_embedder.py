"""A stand-in embedding server for tests: Ollama's and the OpenAI embeddings protocol,
answered by an *oracle* embedding.

No real embedding model can run in the test environment (no network to a model hub), and
the plumbing is what needs testing: requests, batching, caching, detection, the trust gate,
activation, and the readings that follow. The oracle is built from the register anchor file
itself — every word of a dimension's positive exemplars and probes pulls a text toward that
dimension's positive pole, and so on — so it passes the generalization control *by
construction*. That is the point: it lets the tests walk the path a real semantic backend
takes. An "English-only" oracle knows no translated words, so it fails the French, Spanish
and Japanese probes the way an English-only model would.

It is test scaffolding and nothing else: a pass here says the instrument handles a
backend that generalises, not that any model does.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIMS = ("warmth", "moralizing", "distancing", "refusal", "power_seeking")
ANCHORS = Path(__file__).resolve().parents[1] / "corpus" / "register_anchors.toml"
NOISE = 48


def _tokens(text: str) -> list[str]:
    t = (text or "").lower()
    words = re.findall(r"[a-zà-öø-ÿ']+", t)
    cjk = [c for c in t if "぀" <= c <= "鿿"]
    return words + [a + b for a, b in zip(cjk, cjk[1:])]


class Oracle:
    def __init__(self, languages=("en", "fr", "es", "ja")) -> None:
        data = tomllib.loads(ANCHORS.read_text(encoding="utf-8"))
        self.concept: dict[str, list[tuple[int, float]]] = {}
        for i, dim in enumerate(DIMS):
            block = data[dim]
            pos, neg = set(), set()
            for lang in languages:
                sfx = "" if lang == "en" else f"_{lang}"
                keys_pos = ["positive", "probe_positive"] if lang == "en" else [f"probe_positive{sfx}"]
                keys_neg = ["negative", "probe_negative"] if lang == "en" else [f"probe_negative{sfx}"]
                for k in keys_pos:
                    for s in block.get(k, []):
                        pos.update(_tokens(s))
                for k in keys_neg:
                    for s in block.get(k, []):
                        neg.update(_tokens(s))
            for tok in pos - neg:
                self.concept.setdefault(tok, []).append((i, 1.0))
            for tok in neg - pos:
                self.concept.setdefault(tok, []).append((i, -1.0))

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * (len(DIMS) + NOISE)
        for tok in _tokens(text):
            for i, sign in self.concept.get(tok, ()):
                vec[i] += sign
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[len(DIMS) + h % NOISE] += 0.15
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / n * 3.0 for x in vec]      # deliberately NOT unit length: the client normalises


MULTI = Oracle()
ENGLISH = Oracle(languages=("en",))


class FakeEmbedServer:
    """Ollama + OpenAI-compatible endpoints on one local port."""

    def __init__(self) -> None:
        self.installed = {"bge-m3", "nomic-embed-text", "llama3"}
        self.requests: list[tuple[str, dict]] = []
        self.fail_next = 0          # answer this many requests with 503 first
        self.require_key: str | None = None
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._json(200, {"models": [
                        {"name": f"{m}:latest", "details": {"family": "bert" if m != "llama3" else "llama"}}
                        for m in sorted(server.installed)]})
                if self.path == "/v1/models":
                    return self._json(200, {"data": [{"id": "text-embedding-oracle"}, {"id": "chat-model"}]})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                server.requests.append((self.path, body))
                if server.fail_next > 0:
                    server.fail_next -= 1
                    return self._json(503, {"error": "busy"})
                if self.path == "/api/embed":
                    model = body.get("model", "").split(":")[0]
                    if model not in server.installed:
                        return self._json(404, {"error": f"model '{model}' not found, try pulling it first"})
                    oracle = ENGLISH if model == "nomic-embed-text" else MULTI
                    return self._json(200, {"embeddings": [oracle.embed(t) for t in body.get("input", [])]})
                if self.path == "/api/pull":
                    model = body.get("model", "")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for done in (0, 50, 100):
                        self.wfile.write((json.dumps({"status": "pulling", "completed": done, "total": 100}) + "\n").encode())
                    server.installed.add(model.split(":")[0])
                    self.wfile.write(b'{"status":"success"}\n')
                    return
                if self.path in ("/v1/embeddings", "/embeddings"):
                    if server.require_key and self.headers.get("Authorization") != f"Bearer {server.require_key}":
                        return self._json(401, {"error": {"message": "Incorrect API key provided"}})
                    oracle = ENGLISH if "english" in body.get("model", "") else MULTI
                    data = [{"index": i, "embedding": oracle.embed(t)} for i, t in enumerate(body.get("input", []))]
                    return self._json(200, {"data": list(reversed(data))})   # out of order on purpose
                self._json(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def embed_calls(self) -> int:
        return sum(1 for p, _ in self.requests if p in ("/api/embed", "/v1/embeddings", "/embeddings"))

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
