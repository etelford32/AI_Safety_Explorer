"""Semantic embedding backends that need no Python package: models behind an HTTP API.

The in-process backend (`embed_st`, sentence-transformers) needs PyTorch, which is
gigabytes and cannot ship inside the desktop app. These need nothing but the standard
library, so the self-updating app gets them as an ordinary code update, and each one reaches
a model far stronger than anything that would fit in a bundle:

* **Ollama** (`ollama:<model>`) — local, free, private. A model such as `bge-m3` (100+
  languages) or `nomic-embed-text` runs on the same computer; nothing leaves it.
* **OpenAI-compatible** (`openai:<model>`, `lmstudio:<model>`, `compat:<model>@<url>`) —
  OpenAI's own embeddings, and every server that speaks the same protocol: LM Studio,
  llama.cpp's server, vLLM, and most hosted providers.
* **Voyage AI** (`voyage:<model>`) — the embeddings provider Anthropic recommends.

The cloud ones send the text being read to the provider. That is a choice the user makes
explicitly in the UI, with the destination named; the local ones are the default.

Every backend here declares itself semantic — and, exactly as for any other backend, the
declaration is not trusted: `register.generalization` decides, per language, whether its
reading may be believed. A model that turns out not to place paraphrases together is caught
the same way the stdlib fallback is.

Common behaviour: requests are batched; a text longer than one request comfortably holds is
cut at paragraph boundaries and its pieces' vectors averaged (the register is a property of
the whole reply, and truncation would read only its opening); transient failures (429, 5xx,
a dropped connection) are retried with backoff; every vector comes back L2-normalised, so
the instrument's cosine-is-a-dot-product assumption holds whatever the provider returns.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

#: Characters per piece when a long text is split. Well inside every provider's token limit
#: (≈ 1,500 tokens of English) while keeping most replies to one piece.
PIECE_CHARS = 6000
#: Texts per request.
BATCH = 64
RETRIES = 4


class RemoteError(RuntimeError):
    """A provider refused or failed; the message says which and why, without any key."""


def ssl_context():
    """Platform roots and certifi's: a bundled Python often has no system roots at all."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


def http_json(url: str, payload: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None,
              timeout: float = 60.0, retries: int = RETRIES, method: str | None = None) -> Any:
    """POST (or GET, with no payload) JSON and return the decoded answer, retrying what can
    succeed on a second try and failing with a readable message otherwise."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "ai-safety-explorer"}
    hdrs.update(headers or {})
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method or ("POST" if data is not None else "GET"))
        try:
            ctx = ssl_context() if url.startswith("https:") else None
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as exc:
            body = exc.read()[:600].decode("utf-8", "replace")
            if exc.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay *= 2
                continue
            raise RemoteError(f"{url.split('?')[0]} answered {exc.code}: {_short(body)}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            # Nothing listening is not a blip: one quick second try, not the full backoff.
            refused = isinstance(getattr(exc, "reason", exc), ConnectionRefusedError)
            if refused and attempt >= 1:
                retries = attempt
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            reason = getattr(exc, "reason", exc)
            raise RemoteError(f"could not reach {url.split('?')[0]}: {reason}") from None
    raise RemoteError(f"{url} did not answer")


def _short(body: str) -> str:
    try:
        j = json.loads(body)
        msg = j.get("error") if isinstance(j, dict) else None
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("type")
        return str(msg or j)[:300]
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:300]


def _normalise(vec: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in vec))
    return [float(x) / n for x in vec] if n > 1e-12 else [float(x) for x in vec]


def pieces(text: str, limit: int = PIECE_CHARS) -> list[str]:
    """Cut a long text at paragraph (then sentence, then hard) boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            cut = para.rfind(". ", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            out.append(para[:cut])
            para = para[cut:].lstrip()
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [p for p in out if p.strip()] or [text[:limit]]


class _Remote:
    """What the three providers share: pooling long texts, batching, normalising."""

    semantic = True
    kind = "remote"
    #: Where the text goes, for the UI's consent line. Local backends name the machine.
    destination = "this computer"

    def __init__(self, model: str) -> None:
        if not model:
            raise ValueError("a model name is required")
        self.model = model
        self.dim = 0
        self.calls = 0          # requests made, for the UI and tests

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.model}"

    def fingerprint(self) -> str:
        return self.name

    def _request(self, texts: list[str]) -> list[list[float]]:   # pragma: no cover
        raise NotImplementedError

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Split every text into pieces, embed all pieces in batches, then average each
        # text's pieces back into one vector.
        owners: list[int] = []
        flat: list[str] = []
        for i, t in enumerate(texts):
            ps = pieces(t or " ")
            flat.extend(p if p.strip() else " " for p in ps)
            owners.extend([i] * len(ps))
        vecs: list[list[float]] = []
        for k in range(0, len(flat), BATCH):
            got = self._request(flat[k:k + BATCH])
            self.calls += 1
            if len(got) != len(flat[k:k + BATCH]):
                raise RemoteError(f"{self.name} returned {len(got)} vectors for {len(flat[k:k + BATCH])} texts")
            vecs.extend(got)
        if vecs and not self.dim:
            self.dim = len(vecs[0])
        out: list[list[float] | None] = [None] * len(texts)
        sums: dict[int, list[float]] = {}
        counts: dict[int, int] = {}
        for owner, v in zip(owners, vecs):
            v = _normalise(v)
            if owner in sums:
                sums[owner] = [a + b for a, b in zip(sums[owner], v)]
            else:
                sums[owner] = list(v)
            counts[owner] = counts.get(owner, 0) + 1
        for i in range(len(texts)):
            out[i] = _normalise(sums[i]) if counts.get(i, 0) > 1 else sums[i]
        return out   # type: ignore[return-value]


class OllamaBackend(_Remote):
    """A model served by a local Ollama (https://ollama.com). Private: nothing leaves the
    computer. `/api/embed` takes a batch; the server truncates a piece that is still too
    long for the model rather than failing."""

    kind = "ollama"
    DEFAULT_URL = "http://127.0.0.1:11434"

    def __init__(self, model: str, url: str | None = None, timeout: float = 120.0) -> None:
        super().__init__(model)
        self.url = (url or self.DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.destination = "this computer (Ollama)" if _is_local(self.url) else self.url

    def fingerprint(self) -> str:
        return f"ollama:{self.model}"

    def _request(self, texts: list[str]) -> list[list[float]]:
        got = http_json(f"{self.url}/api/embed", {"model": self.model, "input": texts, "truncate": True},
                        timeout=self.timeout)
        vecs = (got or {}).get("embeddings")
        if not isinstance(vecs, list):
            raise RemoteError(f"Ollama returned no embeddings for {self.model!r}: {_short(json.dumps(got))}")
        return vecs


class OpenAICompatBackend(_Remote):
    """The OpenAI embeddings protocol: OpenAI itself, LM Studio, llama.cpp, vLLM and most
    hosted providers. `base_url` is the part before `/embeddings`."""

    kind = "compat"

    def __init__(self, model: str, base_url: str, api_key: str | None = None,
                 kind: str = "compat", timeout: float = 120.0, dimensions: int | None = None) -> None:
        super().__init__(model)
        self.kind = kind
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.dimensions = dimensions
        self.destination = "this computer" if _is_local(self.base_url) else self.base_url.split("/")[2]

    def fingerprint(self) -> str:
        return f"{self.kind}:{self.model}@{self.base_url}" + (f"#{self.dimensions}" if self.dimensions else "")

    def _request(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body: dict[str, Any] = {"model": self.model, "input": texts}
        if self.dimensions:
            body["dimensions"] = self.dimensions
        got = http_json(f"{self.base_url}/embeddings", body, headers=headers, timeout=self.timeout)
        data = (got or {}).get("data")
        if not isinstance(data, list):
            raise RemoteError(f"{self.base_url} returned no embeddings: {_short(json.dumps(got))}")
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]


class VoyageBackend(_Remote):
    """Voyage AI (https://www.voyageai.com), the embeddings provider Anthropic recommends.
    Texts are embedded as documents: the register axis compares replies with exemplar
    replies, a document-to-document similarity."""

    kind = "voyage"
    URL = "https://api.voyageai.com/v1"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 120.0) -> None:
        super().__init__(model)
        if not api_key:
            raise ValueError("Voyage needs an API key (VOYAGE_API_KEY, or enter it in Semantic reading)")
        self.api_key = api_key
        self.base_url = (base_url or self.URL).rstrip("/")
        self.timeout = timeout
        self.destination = "Voyage AI (api.voyageai.com)" if base_url is None else self.base_url

    def _request(self, texts: list[str]) -> list[list[float]]:
        got = http_json(f"{self.base_url}/embeddings",
                        {"input": texts, "model": self.model, "input_type": "document"},
                        headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
        data = (got or {}).get("data")
        if not isinstance(data, list):
            raise RemoteError(f"Voyage returned no embeddings: {_short(json.dumps(got))}")
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]


def _is_local(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# --------------------------------------------------------------------------- discovery


def ollama_models(url: str | None = None, timeout: float = 0.8) -> list[dict[str, Any]] | None:
    """The models a running Ollama has, or None when none is running."""
    base = (url or OllamaBackend.DEFAULT_URL).rstrip("/")
    try:
        got = http_json(f"{base}/api/tags", timeout=timeout, retries=0)
    except RemoteError:
        return None
    out = []
    for m in (got or {}).get("models") or []:
        name = m.get("name") or m.get("model") or ""
        details = m.get("details") or {}
        fam = " ".join([details.get("family") or "", *(details.get("families") or [])]).lower()
        out.append({"name": name, "size": m.get("size"),
                    "embedding": looks_like_embedding_model(name) or "bert" in fam})
    return out


def compat_models(base_url: str, timeout: float = 0.8, api_key: str | None = None) -> list[str] | None:
    """Model ids an OpenAI-compatible server lists, or None when it is not running."""
    try:
        got = http_json(f"{base_url.rstrip('/')}/models", timeout=timeout, retries=0,
                        headers={"Authorization": f"Bearer {api_key}"} if api_key else None)
    except RemoteError:
        return None
    return [m.get("id") for m in (got or {}).get("data") or [] if m.get("id")]


def looks_like_embedding_model(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in ("embed", "bge", "e5-", "gte", "minilm", "mpnet", "nomic", "mxbai",
                                "arctic", "jina", "sentence", "retriev"))


def ollama_pull(model: str, url: str | None = None, on_progress=None, timeout: float = 3600.0) -> None:
    """Ask a running Ollama to download a model, reporting progress as it streams."""
    base = (url or OllamaBackend.DEFAULT_URL).rstrip("/")
    req = urllib.request.Request(f"{base}/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("error"):
                    raise RemoteError(f"Ollama could not pull {model!r}: {ev['error']}")
                if on_progress:
                    on_progress(ev.get("status") or "", ev.get("completed"), ev.get("total"))
    except urllib.error.HTTPError as exc:
        raise RemoteError(f"Ollama could not pull {model!r}: {_short(exc.read().decode('utf-8', 'replace'))}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RemoteError(f"could not reach Ollama at {base}: {getattr(exc, 'reason', exc)}") from None
