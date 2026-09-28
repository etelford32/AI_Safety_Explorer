"""Which embedding backend reads register — chosen in the app, not in an environment variable.

`EXPLORER_EMBED_BACKEND` was the only knob, which is fine in a terminal and useless in a
double-clicked app. The choice now lives in `embedding.json` in the data directory, made
from the UI (or `explorer embed`), and is *earned*: a backend is tested against the register
controls before it becomes active, and the languages it passed are recorded with it.

A backend is named by a spec:

    hashing                          the stdlib fallback (lexical; never trusted)
    minilm[:<model>]                 sentence-transformers in-process (source installs)
    ollama:<model>                   a local Ollama model, e.g. ollama:bge-m3
    lmstudio:<model>                 a local LM Studio model
    openai:<model>                   OpenAI, e.g. openai:text-embedding-3-large
    voyage:<model>                   Voyage AI, e.g. voyage:voyage-3.5
    compat:<model>@<base url>        any OpenAI-compatible server

API keys never go in the spec. They come from the environment (`OPENAI_API_KEY`,
`VOYAGE_API_KEY`, `EXPLORER_COMPAT_API_KEY`) or from `secrets.json` beside the database,
written owner-only (0600) and never sent back to the page.

`EXPLORER_EMBED_BACKEND`, when set, still wins — a terminal user or a test keeps full control.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import embed as embed_mod

CONFIG_NAME = "embedding.json"
SECRETS_NAME = "secrets.json"
CACHE_NAME = "embeddings.sqlite"

#: Where the config, secrets and cache live. The server points this at its database's
#: folder; otherwise it is the data directory.
_HOME: Path | None = None
_LOCK = threading.Lock()
_BUILT: dict[str, Any] = {}          # spec -> backend (cached wrapper)
_CFG_CACHE: tuple[float, dict[str, Any]] | None = None

KEY_ENV = {"openai": "OPENAI_API_KEY", "voyage": "VOYAGE_API_KEY", "compat": "EXPLORER_COMPAT_API_KEY"}
OPENAI_URL = "https://api.openai.com/v1"
LMSTUDIO_URL = "http://127.0.0.1:1234/v1"

#: What the UI offers, with honest one-line descriptions. Sizes are Ollama's downloads.
RECOMMENDED_OLLAMA = [
    {"model": "bge-m3", "size": "≈1.2 GB", "languages": "100+ languages",
     "note": "Recommended. Strong, multilingual, long inputs — reads the French, Spanish and Japanese arms too."},
    {"model": "nomic-embed-text", "size": "≈270 MB", "languages": "English",
     "note": "Small and fast; English only."},
    {"model": "mxbai-embed-large", "size": "≈670 MB", "languages": "English",
     "note": "Strong English model."},
]
CLOUD = {
    "openai": {"label": "OpenAI", "models": ["text-embedding-3-large", "text-embedding-3-small"],
               "destination": "OpenAI (api.openai.com)", "key_url": "https://platform.openai.com/api-keys"},
    "voyage": {"label": "Voyage AI", "models": ["voyage-3.5", "voyage-3-large", "voyage-3.5-lite"],
               "destination": "Voyage AI (api.voyageai.com)", "key_url": "https://dash.voyageai.com/"},
}


# --------------------------------------------------------------------------- files


def set_home(path: str | Path) -> None:
    global _HOME, _CFG_CACHE
    _HOME = Path(path)
    _CFG_CACHE = None
    _BUILT.clear()


def home() -> Path:
    if _HOME is not None:
        return _HOME
    from . import paths
    return paths.data_dir()


def config() -> dict[str, Any]:
    """The saved choice, re-read only when the file changes."""
    global _CFG_CACHE
    p = home() / CONFIG_NAME
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _CFG_CACHE and _CFG_CACHE[0] == mtime:
        return _CFG_CACHE[1]
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    _CFG_CACHE = (mtime, cfg)
    return cfg


def _write_config(cfg: dict[str, Any]) -> None:
    global _CFG_CACHE
    p = home() / CONFIG_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    _CFG_CACHE = None


def _secrets() -> dict[str, str]:
    try:
        return json.loads((home() / SECRETS_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def set_key(provider: str, key: str | None) -> None:
    """Store (or, with None/"", forget) a provider's API key, owner-readable only."""
    if provider not in KEY_ENV:
        raise ValueError(f"unknown provider {provider!r}")
    data = _secrets()
    if key:
        data[provider] = key.strip()
    else:
        data.pop(provider, None)
    p = home() / SECRETS_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(p, 0o600)
    _BUILT.clear()


def key_for(provider: str) -> str | None:
    return os.environ.get(KEY_ENV.get(provider, "")) or _secrets().get(provider) or None


def has_key(provider: str) -> bool:
    return bool(key_for(provider))


# --------------------------------------------------------------------------- specs


def parse(spec: str) -> tuple[str, str, str | None]:
    """spec -> (kind, model, base url or None)."""
    spec = (spec or "hashing").strip()
    if ":" not in spec:
        return spec, "", None
    kind, rest = spec.split(":", 1)
    if kind == "compat":
        if "@" not in rest:
            raise ValueError("compat needs a base URL: compat:<model>@<url>")
        model, url = rest.rsplit("@", 1)
        return kind, model, url
    return kind, rest, None


def _cache_path() -> Path:
    return home() / CACHE_NAME


def build(spec: str, *, cached: bool = True):
    """A backend for a spec. Remote ones are wrapped in the persistent cache."""
    kind, model, url = parse(spec)
    if kind == "hashing":
        return embed_mod.get_backend("hashing")
    if kind == "minilm":
        return embed_mod.get_backend("minilm", **({"model_name": model} if model else {}))
    from . import embed_remote as er
    if kind == "ollama":
        inner = er.OllamaBackend(model, url=config().get("ollama_url") or _ollama_env())
    elif kind == "openai":
        key = key_for("openai")
        if not key:
            raise ValueError("OpenAI needs an API key (OPENAI_API_KEY, or enter it in Semantic reading)")
        inner = er.OpenAICompatBackend(model, OPENAI_URL, api_key=key, kind="openai")
    elif kind == "lmstudio":
        inner = er.OpenAICompatBackend(model, config().get("lmstudio_url") or LMSTUDIO_URL, kind="lmstudio")
    elif kind == "compat":
        inner = er.OpenAICompatBackend(model, url, api_key=key_for("compat"), kind="compat")
    elif kind == "voyage":
        inner = er.VoyageBackend(model, api_key=key_for("voyage"))
    elif kind in embed_mod._BACKENDS:
        return embed_mod.get_backend(kind)
    else:
        raise ValueError(f"unknown embedding backend {spec!r}")
    if not cached:
        return inner
    from .embed_cache import CachedBackend
    return CachedBackend(inner, _cache_path())


def _ollama_env() -> str | None:
    h = os.environ.get("OLLAMA_HOST")
    if not h:
        return None
    return h if h.startswith("http") else f"http://{h}"


def active_spec() -> str:
    return os.environ.get("EXPLORER_EMBED_BACKEND") or config().get("spec") or "hashing"


def fingerprint() -> str:
    """What the readings depend on: the backend, and the languages it was trusted for.
    Anything cached against a reading (the conversation triage) is stale when this moves."""
    cfg = config()
    spec = active_spec()
    if _STATE.get("build_error"):
        # Readings taken while the backend is unreachable are lexical; mark them so they are
        # re-read once it is back.
        return f"{spec}|down"
    langs = ",".join(cfg.get("languages") or []) if cfg.get("spec") == spec else "?"
    return f"{spec}|{langs}"


def backend_for_register():
    """The backend the process-wide register model should use — never raising: a backend
    that cannot be built (Ollama stopped, a key revoked) falls back to hashing, which is
    never trusted, and says so in `status()`."""
    spec = active_spec()
    with _LOCK:
        if spec in _BUILT:
            return _BUILT[spec]
        try:
            b = build(spec)
        except Exception as exc:  # noqa: BLE001
            _STATE["build_error"] = f"{spec}: {exc}"
            b = embed_mod.get_backend("hashing")
        else:
            _STATE["build_error"] = None
        _BUILT[spec] = b
        return b


_STATE: dict[str, Any] = {"build_error": None, "pull": None}


def note_error(message: str | None) -> None:
    """Record why the chosen backend is not reading right now (None: it is)."""
    _STATE["build_error"] = message


# --------------------------------------------------------------------------- detection


def detect() -> dict[str, Any]:
    """What could read register semantically on this computer, right now."""
    from . import embed_remote as er
    import importlib.util

    out: dict[str, Any] = {}
    ollama_url = config().get("ollama_url") or _ollama_env() or er.OllamaBackend.DEFAULT_URL
    models = er.ollama_models(ollama_url)
    installed = {m["name"].split(":")[0]: m for m in (models or [])}
    out["ollama"] = {
        "running": models is not None, "url": ollama_url,
        "models": [m for m in (models or []) if m["embedding"]],
        "recommended": [dict(r, installed=r["model"] in installed) for r in RECOMMENDED_OLLAMA],
        "install_url": "https://ollama.com/download",
    }
    lm = er.compat_models(config().get("lmstudio_url") or LMSTUDIO_URL)
    out["lmstudio"] = {"running": lm is not None,
                       "models": [m for m in (lm or []) if er.looks_like_embedding_model(m)]}
    out["cloud"] = {k: dict(v, has_key=has_key(k)) for k, v in CLOUD.items()}
    out["minilm"] = {"installed": importlib.util.find_spec("sentence_transformers") is not None}
    return out


# --------------------------------------------------------------------------- evaluation


def evaluate(spec: str) -> dict[str, Any]:
    """Build the backend and run the register controls on it: exemplar coherence, and
    generalization in English and every translated-probe language. Nothing is changed."""
    from . import register as reg
    t0 = time.time()
    try:
        backend = build(spec)
        model = reg.load(backend=backend)
        sep = model.separation()
        gen = {lang: model.generalization(None if lang == "en" else lang)
               for lang in ("en", *reg.PROBE_LANGUAGES)}
        langs = model.trusted_languages()
    except Exception as exc:  # noqa: BLE001 — shown to the user as the reason
        return {"spec": spec, "ok": False, "error": str(exc), "seconds": round(time.time() - t0, 2)}
    return {
        "spec": spec, "ok": True, "backend": backend.name, "semantic": bool(backend.semantic),
        "destination": getattr(backend, "destination", "this computer"),
        "dim": getattr(backend, "dim", None),
        "trusted": "en" in langs, "languages": langs,
        "separation": {"passes": sep["passes"], "worst": sep["worst"],
                       "by_dimension": {d: v["auc"] for d, v in sep["by_dimension"].items()}},
        "generalization": {lang: {"passes": g.get("passes"), "worst_margin": g.get("worst_margin"),
                                  "by_dimension": {d: v["margin"] for d, v in (g.get("by_dimension") or {}).items()}}
                           for lang, g in gen.items()},
        "threshold": reg.GENERALIZATION_MARGIN,
        "seconds": round(time.time() - t0, 2),
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def activate(spec: str, *, force: bool = False, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make `spec` the backend that reads register — if it earned it, or when forced (its
    readings then stay marked untrusted, because trust is recomputed, not stored)."""
    result = result if result and result.get("spec") == spec else evaluate(spec)
    if not result.get("ok"):
        raise ValueError(result.get("error") or "the backend could not be tested")
    if not result["trusted"] and not force and spec != "hashing":
        raise ValueError("this backend did not pass the generalization control in English; "
                         "its readings would not be trusted")
    cfg = dict(config())
    cfg.update({"spec": spec, "trusted": result["trusted"], "languages": result["languages"],
                "evaluated_at": result.get("evaluated_at"), "controls": {
                    "generalization": result["generalization"], "separation": result["separation"],
                    "threshold": result["threshold"]},
                "destination": result.get("destination")})
    _write_config(cfg)
    _BUILT.clear()
    from . import stance as st
    st.reset_register_model()
    return cfg


def status() -> dict[str, Any]:
    cfg = config()
    spec = active_spec()
    mine = cfg.get("spec") == spec
    return {
        "spec": spec,
        "from_env": bool(os.environ.get("EXPLORER_EMBED_BACKEND")),
        "trusted": bool(cfg.get("trusted")) if mine else None,
        "languages": (cfg.get("languages") or []) if mine else [],
        "evaluated_at": cfg.get("evaluated_at") if mine else None,
        "controls": cfg.get("controls") if mine else None,
        "destination": cfg.get("destination") if mine else None,
        "build_error": _STATE.get("build_error"),
        "pull": _STATE.get("pull"),
        "fingerprint": fingerprint(),
    }


def start_pull(model: str) -> dict[str, Any]:
    """Download an Ollama model in the background; progress lands in `status()["pull"]`."""
    from . import embed_remote as er
    if (_STATE.get("pull") or {}).get("running"):
        return _STATE["pull"]
    url = config().get("ollama_url") or _ollama_env()
    st_: dict[str, Any] = {"model": model, "running": True, "status": "starting", "completed": None,
                           "total": None, "error": None}
    _STATE["pull"] = st_

    def work() -> None:
        def prog(status: str, done: Any, total: Any) -> None:
            st_.update(status=status, completed=done, total=total)
        try:
            er.ollama_pull(model, url, on_progress=prog)
            st_.update(status="done")
        except Exception as exc:  # noqa: BLE001
            st_.update(error=str(exc))
        finally:
            st_["running"] = False
    threading.Thread(target=work, name="ollama-pull", daemon=True).start()
    return st_
