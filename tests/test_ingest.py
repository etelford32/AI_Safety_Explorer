"""Ingestion lanes and provenance."""

import json

from safety_explorer import db, ingest


def test_manual_capture_is_tier_b(conn, corpus):
    v = corpus.runnable[0]
    run_id = ingest.capture(conn, corpus, v.id, "A pasted response.",
                            model_label="Some Chat Model", surface="web_chat")
    row = db.query_one(conn, "SELECT * FROM run WHERE id = ?", (run_id,))
    assert row["provenance_tier"] == "B"
    assert row["lane"] == "manual"
    assert row["model_alias_risk"] == 1
    # The limitation must travel with the data, not live only in a README.
    unobs = json.loads(row["unobservable"])
    assert "system_prompt" in unobs and "sampling_parameters" in unobs


def test_capture_computes_features(conn, corpus):
    v = corpus.runnable[0]
    run_id = ingest.capture(conn, corpus, v.id, "$E=mc^2$ and 10 km",
                            model_label="m", surface="web_chat")
    assert db.query_one(conn, "SELECT * FROM feature WHERE run_id = ?", (run_id,))


def test_capture_rejects_unknown_prompt(conn, corpus):
    import pytest
    with pytest.raises(ValueError):
        ingest.capture(conn, corpus, "no.such.prompt", "x", model_label="m")


def test_import_matches_by_hash(conn, corpus, tmp_path):
    v = corpus.runnable[0]
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"prompt": v.text, "response": "r", "model": "m"}) + "\n")
    stats = ingest.import_file(conn, corpus, path)
    assert stats["matched"] == 1 and stats["unmatched"] == 0


def test_unmatched_rows_are_kept_not_dropped(conn, corpus, tmp_path):
    """Silently discarding unmatched imports would hide the cases worth looking at."""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"prompt": "totally unrelated text about cats",
                                "response": "r", "model": "m"}) + "\n")
    stats = ingest.import_file(conn, corpus, path)
    assert stats["unmatched"] == 1
    assert len(ingest.unmatched(conn)) == 1


def test_chatml_import(conn, corpus, tmp_path):
    v = corpus.runnable[0]
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"messages": [
        {"role": "user", "content": v.text},
        {"role": "assistant", "content": "the reply"},
    ], "model": "m"}) + "\n")
    stats = ingest.import_file(conn, corpus, path, fmt="chatml")
    assert stats["matched"] == 1
    row = db.query_one(conn, "SELECT response FROM run WHERE prompt_id = ?", (v.id,))
    assert row["response"] == "the reply"
