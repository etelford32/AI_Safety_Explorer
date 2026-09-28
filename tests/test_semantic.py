"""The semantic embedding backends, the cache, the choice, and the per-language trust gate.

Real models cannot run here, so the backends talk to `fake_embedder` — Ollama's and the
OpenAI protocol, answered by an oracle built from the anchor file. What these tests pin is
the instrument's side: requests are batched and retried, vectors normalised and cached, a
backend is tested before it is used and trusted only in the languages it passes, the
choice reaches every reading without a restart, and an unreachable backend degrades to the
honest fallback instead of hanging or crashing.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from fake_embedder import FakeEmbedServer
from safety_explorer import (embed as embed_mod, embed_cache, embed_config, embed_remote as er, intake,
                             live, register as reg, sessions, stance as st)


@pytest.fixture
def fake():
    f = FakeEmbedServer()
    yield f
    f.stop()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("EXPLORER_EMBED_BACKEND", raising=False)
    for k in ("OPENAI_API_KEY", "VOYAGE_API_KEY", "EXPLORER_COMPAT_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    embed_config.set_home(tmp_path)
    embed_config.note_error(None)
    st.reset_register_model()
    yield tmp_path
    embed_config.set_home(tmp_path / "unused")
    st.reset_register_model()


def _point_ollama(home, url):
    (home / "embedding.json").write_text(json.dumps({"ollama_url": url, "lmstudio_url": f"{url}/v1"}))


# --------------------------------------------------------------------------- backends


def test_long_text_is_split_and_pooled():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 300 for i in range(8))
    parts = er.pieces(text, 2000)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_ollama_batches_and_normalises(fake):
    b = er.OllamaBackend("bge-m3", url=fake.url)
    vecs = b.embed([f"text number {i}" for i in range(150)])
    assert len(vecs) == 150 and fake.embed_calls() == 3          # 64 + 64 + 22
    assert all(abs(sum(x * x for x in v) - 1) < 1e-6 for v in vecs)


def test_openai_compatible_sorts_by_index_and_sends_the_key(fake):
    fake.require_key = "sk-test"
    b = er.OpenAICompatBackend("oracle", f"{fake.url}/v1", api_key="sk-test", kind="openai")
    one, two = b.embed(["Delighted to help you.", "Regrettably I must decline."])
    assert one == er.OllamaBackend("bge-m3", url=fake.url).embed(["Delighted to help you."])[0]
    bad = er.OpenAICompatBackend("oracle", f"{fake.url}/v1", api_key="sk-wrong")
    with pytest.raises(er.RemoteError) as exc:
        bad.embed(["x"])
    assert "401" in str(exc.value) and "Incorrect API key" in str(exc.value) and "sk-wrong" not in str(exc.value)


def test_a_busy_provider_is_retried(fake, monkeypatch):
    monkeypatch.setattr(er.time, "sleep", lambda s: None)
    fake.fail_next = 2
    assert len(er.OllamaBackend("bge-m3", url=fake.url).embed(["hello"])) == 1


def test_voyage_uses_its_protocol(fake):
    b = er.VoyageBackend("voyage-3.5", api_key="pa-test", base_url=f"{fake.url}/v1")
    assert len(b.embed(["a", "b"])) == 2
    path, body = fake.requests[-1]
    assert body["input_type"] == "document" and body["model"] == "voyage-3.5"
    with pytest.raises(ValueError):
        er.VoyageBackend("voyage-3.5", api_key=None)


def test_the_cache_answers_repeats_and_survives_a_restart(fake, tmp_path):
    path = tmp_path / "emb.sqlite"
    c1 = embed_cache.CachedBackend(er.OllamaBackend("bge-m3", url=fake.url), path)
    first = c1.embed(["alpha", "beta", "alpha"])
    assert fake.embed_calls() == 1 and first[0] == first[2]
    c1.embed(["beta", "alpha"])
    assert fake.embed_calls() == 1 and c1.hits >= 2
    c2 = embed_cache.CachedBackend(er.OllamaBackend("bge-m3", url=fake.url), path)
    assert c2.embed(["alpha"])[0] == pytest.approx(first[0], abs=1e-6)
    assert fake.embed_calls() == 1                              # from disk, not the server
    other = embed_cache.CachedBackend(er.OllamaBackend("nomic-embed-text", url=fake.url), path)
    other.embed(["alpha"])
    assert fake.embed_calls() == 2                              # a different model is a different key


def test_minilm_is_registered_when_asked_for_by_name():
    b = embed_mod.get_backend("minilm")
    assert "minilm" in embed_mod._BACKENDS
    # Without sentence-transformers it falls back — and says it is not semantic.
    assert b.semantic is (b.name == "minilm")


# --------------------------------------------------------------------------- choosing


def test_detection_finds_ollama_models_and_what_is_missing(home, fake):
    _point_ollama(home, fake.url)
    fake.installed = {"nomic-embed-text", "llama3"}
    d = embed_config.detect()
    assert d["ollama"]["running"]
    assert [m["name"] for m in d["ollama"]["models"]] == ["nomic-embed-text:latest"]   # llama3 is not an embedder
    rec = {r["model"]: r["installed"] for r in d["ollama"]["recommended"]}
    assert rec["bge-m3"] is False and rec["nomic-embed-text"] is True
    assert d["lmstudio"]["running"] and d["lmstudio"]["models"] == ["text-embedding-oracle"]
    assert d["cloud"]["openai"]["has_key"] is False


def test_nothing_running_is_reported_not_raised(home):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _point_ollama(home, f"http://127.0.0.1:{port}")
    d = embed_config.detect()
    assert d["ollama"]["running"] is False and d["lmstudio"]["running"] is False


def test_a_multilingual_model_is_trusted_in_every_probe_language(home, fake):
    _point_ollama(home, fake.url)
    r = embed_config.evaluate("ollama:bge-m3")
    assert r["ok"] and r["trusted"] and r["languages"] == ["en", "fr", "es", "ja"]
    assert r["destination"].startswith("this computer")
    assert all(g["passes"] for g in r["generalization"].values())


def test_an_english_only_model_is_not_trusted_in_japanese(home, fake):
    _point_ollama(home, fake.url)
    r = embed_config.evaluate("ollama:nomic-embed-text")
    assert r["trusted"] and "en" in r["languages"]
    assert "ja" not in r["languages"] and "fr" not in r["languages"]


def test_the_fallback_is_never_trusted(home):
    r = embed_config.evaluate("hashing")
    assert r["ok"] and not r["trusted"] and r["languages"] == []


def test_a_missing_model_is_a_readable_failure(home, fake):
    _point_ollama(home, fake.url)
    r = embed_config.evaluate("ollama:not-a-model")
    assert not r["ok"] and "not found" in r["error"]


def test_activation_reaches_every_reading_without_a_restart(home, fake):
    _point_ollama(home, fake.url)
    before = st.embedding_reading("Delighted to sit alongside you on this.")
    assert before["trustworthy"] is False
    embed_config.activate("ollama:bge-m3")
    assert st.default_backend_name() == "ollama:bge-m3"
    after = st.embedding_reading("Delighted to sit alongside you on this.", language="ja")
    assert after["trustworthy"] is True and after["backend"] == "ollama:bge-m3"
    assert sorted(st.trusted_languages()) == ["en", "es", "fr", "ja"]
    cfg = json.loads((home / "embedding.json").read_text())
    assert cfg["spec"] == "ollama:bge-m3" and cfg["languages"] == ["en", "fr", "es", "ja"]


def test_per_language_trust_follows_the_model(home, fake):
    _point_ollama(home, fake.url)
    embed_config.activate("ollama:nomic-embed-text")
    assert st.embedding_reading("x", language="en")["trustworthy"] is True
    assert st.embedding_reading("x", language="ja")["trustworthy"] is False


def test_an_untrusted_backend_is_refused_unless_forced(home, fake):
    failed = {"spec": "compat:x@http://127.0.0.1:9", "ok": True, "trusted": False, "languages": [],
              "generalization": {}, "separation": {}, "threshold": 0.05}
    with pytest.raises(ValueError, match="generalization"):
        embed_config.activate(failed["spec"], result=failed)
    embed_config.activate(failed["spec"], force=True, result=failed)
    assert st.default_backend_name() == failed["spec"]
    assert embed_config.status()["trusted"] is False            # forced, never promoted
    embed_config.activate("hashing", force=True)
    assert st.default_backend_name() == "hashing"


def test_the_environment_still_wins(home, fake, monkeypatch):
    _point_ollama(home, fake.url)
    embed_config.activate("ollama:bge-m3")
    monkeypatch.setenv("EXPLORER_EMBED_BACKEND", "hashing")
    assert st.default_backend_name() == "hashing" and embed_config.status()["from_env"]


def test_keys_are_stored_owner_only_and_never_reported(home):
    embed_config.set_key("openai", "sk-secret-123")
    p = home / "secrets.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert embed_config.key_for("openai") == "sk-secret-123" and embed_config.has_key("openai")
    assert "sk-secret" not in json.dumps(embed_config.status())
    embed_config.set_key("openai", "")
    assert not embed_config.has_key("openai")
    with pytest.raises(ValueError, match="API key"):
        embed_config.build("openai:text-embedding-3-large")


def test_a_stopped_backend_degrades_to_the_lexicon_quickly(home, fake):
    _point_ollama(home, fake.url)
    embed_config.activate("ollama:bge-m3")
    fake.stop()
    st.reset_register_model()
    embed_config._BUILT.clear()
    t0 = time.time()
    r = st.embedding_reading("A brand new sentence never embedded before, about kittens.")
    assert time.time() - t0 < 8
    assert r is not None and r["trustworthy"] is False        # read, but by the untrusted fallback
    assert embed_config.status()["build_error"]
    assert embed_config.fingerprint().endswith("|down")


# --------------------------------------------------------------------------- readings


def test_a_french_conversation_drifts_on_the_trusted_embedding(home, fake):
    _point_ollama(home, fake.url)
    embed_config.activate("ollama:bge-m3")
    import tomllib
    anchors = tomllib.loads(reg.anchors_path().read_text(encoding="utf-8"))
    warm = anchors["warmth"]["probe_positive_fr"]
    refuse = anchors["refusal"]["probe_positive_fr"]
    turns = []
    for a in warm + refuse:
        turns += [{"role": "user", "text": "Et ensuite ?"}, {"role": "assistant", "text": a}]
    r = live.analyse_turns(turns, language="fr")
    assert r["drift"]["source"] == "embedding"
    assert any(p["value"] is not None for p in r["drift"].get("trajectory", {}).get("warmth", [])) or r["drift"]["status"]


def test_imported_conversations_carry_their_language(conn, corpus):
    from safety_explorer import intake_store
    b = intake.Batch("messages", "fr.json")
    c = intake.Conversation(key="fr1")
    c.add("user", "Bonjour, pouvez-vous m'expliquer comment la traînée fait décroître une orbite ? Je ne comprends pas.")
    c.add("assistant", "Bien sûr ! La traînée atmosphérique retire de l'énergie, et l'orbite se rétrécit avec le temps.")
    b.conversations.append(intake._finish(c))
    sid = intake_store.import_batches(conn, corpus, [b])["session_ids"][0]
    from safety_explorer import db
    assert db.query_one(conn, "SELECT language FROM live_session WHERE id = ?", (sid,))["language"] == "fr"


def test_choosing_a_backend_makes_every_triage_stale(home, fake, conn):
    sessions.append_turn(conn, "s1", "user", "hello")
    sessions.append_turn(conn, "s1", "assistant", "Delighted to sit alongside you on this.")
    v1 = sessions.summary_version()
    s1 = sessions.summary(conn, "s1")
    assert s1["flags"]["read_by"] == "lexicon"
    _point_ollama(home, fake.url)
    embed_config.activate("ollama:bge-m3")
    assert sessions.summary_version() != v1
    rows = sessions.list_sessions(conn)
    assert rows[0]["pending"] is False
    assert sessions.summary(conn, "s1")["flags"]["read_by"] == "embedding"


def test_the_anchor_file_is_found_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert reg.load().version and reg.lint() == []


# --------------------------------------------------------------------------- over HTTP


@pytest.fixture
def live_server(tmp_path, fake, monkeypatch):
    from safety_explorer import server
    monkeypatch.delenv("EXPLORER_EMBED_BACKEND", raising=False)
    (tmp_path / "embedding.json").write_text(json.dumps({"ollama_url": fake.url}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(tmp_path / "srv.db"), "corpus", "127.0.0.1", port),
                     daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield port
    st.reset_register_model()


def _call(port, method, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_test_then_use_over_http(live_server):
    port = live_server
    _, d = _call(port, "GET", "/api/embedding")
    assert d["status"]["spec"] == "hashing" and d["detect"]["ollama"]["running"]
    _, r = _call(port, "POST", "/api/embedding/test", {"spec": "ollama:bge-m3"})
    assert r["trusted"] and r["languages"] == ["en", "fr", "es", "ja"]
    _, u = _call(port, "POST", "/api/embedding/use", {"spec": "ollama:bge-m3"})
    assert u["ok"] and u["status"]["spec"] == "ollama:bge-m3"
    _, s = _call(port, "GET", "/api/status")
    assert s["embedding"]["spec"] == "ollama:bge-m3" and s["embedding_trustworthy"] is True


def test_a_key_saved_over_http_is_never_returned(live_server):
    port = live_server
    _, r = _call(port, "POST", "/api/embedding/key", {"provider": "voyage", "key": "pa-secret-999"})
    assert r == {"ok": True, "has_key": True}
    _, d = _call(port, "GET", "/api/embedding")
    assert "pa-secret" not in json.dumps(d) and d["detect"]["cloud"]["voyage"]["has_key"]


def test_a_chat_page_cannot_reach_the_embedding_settings(live_server):
    status, _ = _call(live_server, "POST", "/api/embedding/key", {"provider": "openai", "key": "x"},
                      {"Origin": "https://chatgpt.com"})
    assert status == 403
    status, _ = _call(live_server, "GET", "/api/embedding", headers={"Origin": "https://claude.ai"})
    assert status == 403


def test_ollama_pull_reports_progress(live_server, fake):
    port = live_server
    fake.installed.discard("bge-m3")
    _call(port, "POST", "/api/embedding/pull", {"model": "bge-m3"})
    for _ in range(50):
        _, d = _call(port, "GET", "/api/embedding?detect=0")
        if not d["status"]["pull"]["running"]:
            break
        time.sleep(0.1)
    assert d["status"]["pull"]["status"] == "done" and "bge-m3" in fake.installed
