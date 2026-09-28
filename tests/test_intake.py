"""Intake: any conversation data in, recognised by content, stored once.

The fixtures in tests/fixtures/intake/ are built to the real shapes — a ChatGPT export's
message tree, a Claude.ai export, a Claude Code session log (checked against a real one),
a Codex CLI rollout, OpenAI/Anthropic message files, ShareGPT, CSV, a transcript — each
with the awkward parts that break a naive reader: a regenerated branch, hidden system
messages, an assistant message split across lines, tool results posing as user turns.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from safety_explorer import db, intake, intake_store, sessions, sources

FIX = Path(__file__).resolve().parent / "fixtures" / "intake"


def _one(name: str) -> intake.Batch:
    got = [b for b in intake.detect_path(FIX / name) if b.conversations or b.pairs]
    assert len(got) == 1, [b.describe() for b in got]
    return got[0]


def _roles(conv):
    return [t["role"] for t in conv.turns]


# --------------------------------------------------------------------------- detection


@pytest.mark.parametrize("name,fmt,tier", [
    ("chatgpt_conversations.json", "chatgpt_export", "B"),
    ("claude_conversations.json", "claude_export", "B"),
    ("claude_code.jsonl", "claude_code", "B"),
    ("codex.jsonl", "codex_cli", "B"),
    ("eval_messages.jsonl", "messages", "C"),
    ("sharegpt.json", "sharegpt", "C"),
    ("pairs.csv", "pairs", "C"),
    ("transcript.md", "transcript", "C"),
    ("generic_lmstudio.json", "messages", "C"),
])
def test_each_format_is_recognised_by_its_content(name, fmt, tier):
    b = _one(name)
    assert b.format == fmt and b.tier == tier
    assert b.source.startswith("import:")


def test_detection_ignores_the_file_name(tmp_path):
    # A Claude.ai export renamed to look like anything else is still a Claude.ai export.
    odd = tmp_path / "data.txt"
    shutil.copy(FIX / "claude_conversations.json", odd)
    assert [b.format for b in intake.detect_path(odd)] == ["claude_export"]


def test_chatgpt_reads_the_branch_that_was_on_screen():
    conv = _one("chatgpt_conversations.json").conversations[0]
    texts = " ".join(t["text"] for t in conv.turns)
    assert "regenerated" not in texts                       # the discarded draft
    assert "square of the number density" in texts           # the kept reply
    assert _roles(conv) == ["system", "user", "assistant", "tool", "tool", "user", "assistant"]
    assert conv.turns[0]["text"].startswith("[custom instructions]")
    assert "[image]" in conv.turns[1]["text"]
    assert conv.model == "gpt-4o" and conv.key == "c-111"
    assert conv.created_at.startswith("2023-11-14")
    assert conv.meta["branches_not_shown"] == 1


def test_chatgpt_counts_what_it_leaves_out():
    b = _one("chatgpt_conversations.json")
    assert any("hidden system" in n for n in b.notes)
    assert any("reasoning" in n for n in b.notes)
    assert b.skipped == 1   # the conversation with no messages


def test_claude_code_puts_a_split_message_back_together():
    conv = _one("claude_code.jsonl").conversations[0]
    assert _roles(conv) == ["user", "assistant", "tool", "tool", "assistant"]
    assert conv.turns[1]["text"] == "Let me look at the test.\n\nIt asserts the wrong value."
    assert conv.turns[2]["text"].startswith("→ Bash:")
    assert conv.turns[3]["text"].startswith("← result: 1 failed")      # a tool result, not the user
    assert all("sidechain" not in t["text"] and "/clear" not in t["text"] for t in conv.turns)
    assert conv.title == "proj — Fix the parser"                     # from the summary line
    assert conv.model == "claude-test" and conv.key == "sess-cc"


def test_codex_keeps_the_conversation_and_drops_the_environment_block():
    conv = _one("codex.jsonl").conversations[0]
    assert _roles(conv) == ["user", "tool", "tool", "assistant"]
    assert "a.py" in conv.turns[2]["text"]
    assert conv.model == "gpt-5-codex" and conv.key == "cx-9"


def test_messages_fold_in_system_and_trailing_response():
    b = _one("eval_messages.jsonl")
    first, second = b.conversations
    assert _roles(first) == ["system", "user", "assistant"] and first.turns[-1]["text"] == "4."
    assert _roles(second) == ["user", "assistant", "tool", "tool", "assistant"]


def test_an_unsplittable_text_is_not_imported_as_a_conversation():
    (b,) = intake.detect_path(FIX / "not_a_conversation.txt")
    assert not b.conversations and b.notes and "could not be told apart" in b.notes[0]


def test_an_export_zip_reads_the_json_and_skips_the_rest(tmp_path):
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(FIX / "chatgpt_conversations.json", "conversations.json")
        zf.writestr("chat.html", "<html>same data again</html>")
        zf.writestr("user.json", json.dumps({"email": "someone@example.com"}))
        zf.writestr("image.png", b"\x89PNG")
    got = intake.detect_path(z)
    assert [b.format for b in got] == ["chatgpt_export"]
    assert got[0].origin == "export.zip/conversations.json"
    # Bytes too, as an upload arrives.
    assert [b.format for b in intake.detect_bytes("export.zip", z.read_bytes())] == ["chatgpt_export"]


def test_a_huge_tool_result_is_clipped_and_says_so():
    conv = intake.Conversation(key="k")
    conv.add("user", "x" * (intake.MAX_TURN_CHARS + 50))
    assert conv.turns[0]["text"].endswith("more characters]")


def test_timestamps_in_every_shape():
    assert intake.iso(1700000000) == "2023-11-14T22:13:20+00:00"
    assert intake.iso(1700000000000) == "2023-11-14T22:13:20+00:00"
    assert intake.iso("2025-03-01T10:00:00.000000Z") == "2025-03-01T10:00:00+00:00"
    assert intake.iso("not a date") is None and intake.iso(None) is None


# --------------------------------------------------------------------------- storing


def _all_batches():
    return [b for f in sorted(FIX.iterdir()) for b in intake.detect_path(f)]


def test_import_is_idempotent(conn, corpus):
    first = intake_store.import_batches(conn, corpus, _all_batches())
    assert first["new"] == first["conversations"] > 0
    again = intake_store.import_batches(conn, corpus, _all_batches())
    assert again["new"] == again["turns_added"] == again["runs_added"] == 0
    assert again["unchanged"] == first["conversations"]


def test_a_conversation_that_grew_gets_only_its_new_turns(conn, corpus):
    b = _one("claude_code.jsonl")
    intake_store.import_batches(conn, corpus, [b])
    sid = intake_store.session_id_for(b, b.conversations[0])
    b.conversations[0].add("user", "One more thing.")
    b.conversations[0].add("assistant", "Done.")
    res = intake_store.import_batches(conn, corpus, [b])
    assert res["updated"] == 1 and res["turns_added"] == 2
    assert len(sessions.session_turns(conn, sid)) == 7


def test_an_edited_conversation_becomes_a_branch_not_a_rewrite(conn, corpus):
    b = _one("claude_conversations.json")
    intake_store.import_batches(conn, corpus, [b])
    sid = intake_store.session_id_for(b, b.conversations[0])
    before = [t["text"] for t in sessions.session_turns(conn, sid)]
    b.conversations[0].turns[1]["text"] = "A regenerated answer."
    res = intake_store.import_batches(conn, corpus, [b])
    assert res["branched"] == 1
    assert [t["text"] for t in sessions.session_turns(conn, sid)] == before
    assert len(sessions.session_turns(conn, f"{sid}~b1")) == len(before)


def _keyed_variant(corpus):
    from safety_explorer.groundtruth import families_with_ground_truth
    fams = set(families_with_ground_truth())
    return next(v for v in corpus.runnable if v.family_id in fams and v.id.endswith(".C"))


def test_a_conversation_that_opens_with_a_corpus_prompt_becomes_a_scored_run(conn, corpus):
    v = _keyed_variant(corpus)
    b = intake.Batch("chatgpt_export", "t.json")
    conv = intake.Conversation(key="opens", model="gpt-x")
    conv.add("user", v.text)
    conv.add("assistant", "Here is the worked answer.")
    b.conversations.append(intake._finish(conv))
    res = intake_store.import_batches(conn, corpus, [b])
    assert res["runs_added"] == 1
    run = db.query_one(conn, "SELECT * FROM run WHERE lane = 'import'")
    assert run["prompt_id"] == v.id and run["provenance_tier"] == "B" and run["surface"] == "web_chat"
    assert db.query_one(conn, "SELECT 1 AS x FROM ground_truth WHERE run_id = ?", (run["id"],))
    assert db.loads(run["params"])["from_session"] == res["session_ids"][0]
    # Re-importing never stores it twice.
    assert intake_store.import_batches(conn, corpus, [b])["runs_added"] == 0


def test_a_corpus_prompt_asked_mid_conversation_is_not_a_run(conn, corpus):
    v = _keyed_variant(corpus)
    b = intake.Batch("chatgpt_export", "t.json")
    conv = intake.Conversation(key="mid")
    conv.add("user", "Hello there.")
    conv.add("assistant", "Hi.")
    conv.add("user", v.text)
    conv.add("assistant", "An answer with context the corpus prompt never had.")
    b.conversations.append(intake._finish(conv))
    assert intake_store.import_batches(conn, corpus, [b])["runs_added"] == 0


def test_pairs_go_to_runs_when_they_match_and_to_conversations_when_not(conn, corpus):
    v = _keyed_variant(corpus)
    b = intake.Batch("pairs", "p.csv")
    b.pairs = [{"prompt": v.text, "response": "an answer", "key": "r0", "row": 0},
               {"prompt": "Unrelated question?", "response": "Unrelated answer.", "key": "r1", "row": 1}]
    res = intake_store.import_batches(conn, corpus, [b])
    assert res["pairs_as_runs"] == 1 and res["pairs_as_conversations"] == 1


def test_the_preview_counts_without_storing(conn, corpus):
    pv = intake_store.preview(_all_batches(), corpus)
    assert pv["n_conversations"] > 0 and not pv["empty"]
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM live_session")["n"] == 0


# --------------------------------------------------------------------------- triage


def test_triage_flags_a_refusal_and_caches_the_reading(conn, corpus):
    b = _one("chatgpt_conversations.json")
    res = intake_store.import_batches(conn, corpus, [b])
    sid = res["session_ids"][0]
    s1 = sessions.summary(conn, sid, corpus)
    assert s1["flags"]["refusals"] == 1 and s1["score"] > 0
    stamp = db.query_one(conn, "SELECT computed_at FROM session_summary WHERE session_id = ?", (sid,))
    time.sleep(1.1)
    sessions.summary(conn, sid, corpus)
    assert db.query_one(conn, "SELECT computed_at FROM session_summary WHERE session_id = ?", (sid,)) == stamp
    sessions.append_turn(conn, sid, "user", "and now?")
    sessions.summary(conn, sid, corpus)
    assert db.query_one(conn, "SELECT n_turns FROM session_summary WHERE session_id = ?", (sid,))["n_turns"] == 8


def test_the_list_filters_and_puts_the_flagged_first(conn, corpus):
    intake_store.import_batches(conn, corpus, _all_batches())
    sessions.append_turn(conn, "live-1", "user", "a live turn", source="agent")
    rows, total = sessions.list_sessions(conn, order="interest", corpus=corpus, with_total=True)
    assert total == 12 and rows[0]["label"] == "Debris cascade"
    assert all(r["source"] == "agent" for r in sessions.list_sessions(conn, source="live"))
    imported = sessions.list_sessions(conn, source="imported")
    assert len(imported) == 11 and all(r["source"].startswith("import:") for r in imported)
    flagged = sessions.list_sessions(conn, flagged=True)
    assert [r["label"] for r in flagged] == ["Debris cascade"]
    assert [r["label"] for r in sessions.list_sessions(conn, q="decay")] == ["Orbital decay question"]


def test_a_long_session_is_read_over_a_stated_window(conn, monkeypatch):
    monkeypatch.setattr(sessions, "DETAIL_WINDOW", 4)
    for i in range(6):
        sessions.append_turn(conn, "long", "user", f"question {i}")
        sessions.append_turn(conn, "long", "tool", f"tool output {i}")
        sessions.append_turn(conn, "long", "assistant", f"answer {i}")
    r = sessions.analyse_session(conn, "long")
    assert r["window"] == {"shown": 4, "total": 12, "tool_turns": 6}
    assert len(r["positions"]) == 4 and r["positions"][0] == 12
    assert [c["pos"] for c in r["context_turns"]] == [13, 16]
    assert "most recent 4" in r["limits"][1]


# --------------------------------------------------------------------------- sources


def test_discovery_reads_names_only_and_finds_known_places(tmp_path, monkeypatch, conn):
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "claude_code.jsonl", proj / "sess-cc.jsonl")
    (home / "Downloads").mkdir()
    with zipfile.ZipFile(home / "Downloads" / "abc-2025.zip", "w") as zf:
        zf.write(FIX / "chatgpt_conversations.json", "conversations.json")
        zf.writestr("chat.html", "")
    with zipfile.ZipFile(home / "Downloads" / "photos.zip", "w") as zf:
        zf.writestr("a.jpg", b"x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    found = sources.discover(conn)
    assert [k["kind"] for k in found["known"]] == ["claude_code"]
    assert found["known"][0]["n_files"] == 1 and not found["known"][0]["connected"]
    assert [(e["name"], e["kind"]) for e in found["exports"]] == [("abc-2025.zip", "ChatGPT export")]
    # Nothing was imported by looking.
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM live_session")["n"] == 0


def _worker(tmp_path, corpus, db_path):
    w = sources.IntakeWorker(str(db_path), corpus, tmp_path / "inbox", interval=0.3).start()
    return w


def _wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_the_inbox_imports_what_is_dropped_and_files_it(tmp_path, corpus):
    path = tmp_path / "x.db"
    db.init_db(path).close()
    w = _worker(tmp_path, corpus, path)
    try:
        shutil.copy(FIX / "claude_conversations.json", w.inbox / "export.json")
        shutil.copy(FIX / "not_a_conversation.txt", w.inbox / "notes.txt")
        assert _wait(lambda: (w.inbox / "imported" / "export.json").exists()
                     and (w.inbox / "failed" / "notes.txt").exists())
        c = db.connect(path)
        assert db.query_one(c, "SELECT COUNT(*) AS n FROM live_session WHERE source = 'import:claude'")["n"] == 1
        statuses = {e["origin"]: e["status"] for e in intake_store.recent_events(c)}
        assert statuses == {"inbox/export.json": "ok", "inbox/notes.txt": "empty"}
    finally:
        w.stop()


def test_a_connected_source_picks_up_new_turns_as_the_log_grows(tmp_path, corpus):
    path = tmp_path / "x.db"
    db.init_db(path).close()
    logs = tmp_path / "projects" / "-work-proj"
    logs.mkdir(parents=True)
    log = logs / "sess-cc.jsonl"
    shutil.copy(FIX / "claude_code.jsonl", log)
    w = _worker(tmp_path, corpus, path)
    try:
        c = db.connect(path)
        src = sources.connect_source(c, "claude_code", str(tmp_path / "projects"))
        w.submit_scan(src["id"])
        n = lambda: db.query_one(c, "SELECT COUNT(*) AS n FROM live_turn")["n"]  # noqa: E731
        assert _wait(lambda: n() == 5)
        with log.open("a") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": "sess-cc", "uuid": "11",
                                 "timestamp": "2025-06-01T09:01:00Z",
                                 "message": {"role": "user", "content": "Thanks!"}}) + "\n")
        assert _wait(lambda: n() == 6)
        assert db.query_one(c, "SELECT COUNT(*) AS n FROM live_session")["n"] == 1
        sources.disconnect_source(c, src["id"])
        assert not db.query_one(c, "SELECT watch FROM intake_source WHERE id = ?", (src["id"],))["watch"]
    finally:
        w.stop()


# --------------------------------------------------------------------------- over HTTP


@pytest.fixture
def live_server(tmp_path, corpus):
    from safety_explorer import server
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(tmp_path / "srv.db"), "corpus", "127.0.0.1", port),
                     daemon=True).start()
    assert _wait(lambda: _up(port), 10)
    return port


def _up(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1)
        return True
    except OSError:
        return False


def _call(port, method, path, data=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_upload_preview_then_commit(live_server):
    port = live_server
    body = (FIX / "chatgpt_conversations.json").read_bytes()
    status, pv = _call(port, "POST", "/api/intake/upload", body,
                       {"X-Filename": "conversations.json", "Content-Type": "application/octet-stream"})
    assert status == 200 and pv["n_conversations"] == 1 and pv["batches"][0]["format"] == "chatgpt_export"
    _, got = _call(port, "GET", "/api/sessions")
    assert got["total"] == 0                                    # a preview stores nothing
    status, _ = _call(port, "POST", "/api/intake/commit",
                      json.dumps({"token": pv["token"], "name": "conversations.json"}).encode(),
                      {"Content-Type": "application/json"})
    assert status == 200
    assert _wait(lambda: _call(port, "GET", "/api/sessions")[1]["total"] == 1)
    _, st = _call(port, "GET", "/api/intake")
    assert st["events"][0]["origin"] == "conversations.json" and st["events"][0]["status"] == "ok"


def test_pasted_text_is_detected_too(live_server):
    _, pv = _call(live_server, "POST", "/api/intake/text",
                  json.dumps({"text": (FIX / "transcript.md").read_text()}).encode(),
                  {"Content-Type": "application/json"})
    assert pv["n_conversations"] == 1 and pv["batches"][0]["format"] == "transcript"


def test_a_chat_page_cannot_reach_intake(live_server):
    status, _ = _call(live_server, "POST", "/api/intake/upload", b"{}",
                      {"Origin": "https://chatgpt.com", "X-Filename": "x.json"})
    assert status == 403
    status, _ = _call(live_server, "GET", "/api/intake/discover", headers={"Origin": "https://claude.ai"})
    assert status == 403


def test_a_commit_cannot_name_a_file_outside_the_uploads(live_server):
    status, body = _call(live_server, "POST", "/api/intake/commit",
                         json.dumps({"token": "../../x.db"}).encode(), {"Content-Type": "application/json"})
    assert status == 400 and "unknown" in body["error"]


def test_the_cli_imports_a_folder(tmp_path, capsys):
    from safety_explorer import cli
    rc = cli.main(["--db", str(tmp_path / "c.db"), "import", str(FIX)])
    out = capsys.readouterr().out
    assert rc == 0 and "ChatGPT export (Tier B)" in out and "worth a look" in out
