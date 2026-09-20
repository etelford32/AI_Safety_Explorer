"""Live conversation analysis — parsing pasted text while a model is open elsewhere.

The tests that matter here guard the honesty of the thing, not its cleverness: that a
wrong turn split is reported rather than acted on, and that a descriptive reading always
arrives with the limits that keep it from being mistaken for a controlled measurement.
"""

from __future__ import annotations

from safety_explorer import live


# --- turn splitting --------------------------------------------------------

def test_speaker_markers_split_into_alternating_turns():
    r = live.split_turns("User: what is the flux?\nAssistant: about 3 per year.")
    assert r["confident"]
    assert [t["role"] for t in r["turns"]] == ["user", "assistant"]


def test_a_json_messages_array_is_parsed_and_trusted():
    r = live.split_turns('{"messages": [{"role": "user", "content": "hi"}, '
                         '{"role": "assistant", "content": "hello"}]}')
    assert r["convention"] == "json messages"
    assert r["confident"]
    assert len(r["turns"]) == 2


def test_unmarked_text_is_ONE_assistant_turn_and_says_so():
    """A wrong split attributes the model's register to the user, or the reverse.

    So text with no markers is not guessed at: it is read as a single assistant turn and
    flagged not-confident, with an instruction to label the turns. Silently splitting on
    blank lines or question marks would invert every reading that follows.
    """
    r = live.split_turns("Here is a long answer.\n\nWith two paragraphs.\n\nAnd a third.")
    assert not r["confident"]
    assert len(r["turns"]) == 1
    assert r["turns"][0]["role"] == "assistant"
    assert "wrong split" in r["note"]


def test_one_sided_markers_are_reported_not_confident():
    """Markers matched in only one direction produce same-role turns.

    That is the commonest real failure — a transcript that labels "Me:" but not the
    model — and it must not pass as a clean split.
    """
    r = live.split_turns("Me: first question\nMe: second question")
    assert not r["confident"]


# --- what runs, and what refuses to ---------------------------------------

def test_register_is_scored_per_assistant_turn():
    conv = ("User: q1\nAssistant: Let's work through this together, we can take your "
            "numbers directly.\nUser: q2\nAssistant: It is important to note that one "
            "would defer to the relevant authorities here.")
    r = live.analyse(conv)
    a = [t for t in r["turns"] if t["role"] == "assistant"]
    assert a[0]["stance"]["warmth"] > 0
    assert a[1]["stance"]["moralizing"] > 0


def test_the_trajectory_is_the_point_and_moves_across_turns():
    """The one thing a campaign cannot show: register as the conversation is pushed."""
    conv = ("User: benign\nAssistant: Let's work through this together happily.\n"
            "User: loaded\nAssistant: I want to be careful. It is important to note the limits.\n"
            "User: pushed\nAssistant: I'm not able to help with that.")
    r = live.analyse(conv)
    warmth = [p["value"] for p in r["trajectory"]["warmth"]]
    refusal = [p["value"] for p in r["trajectory"]["refusal_rate"]]
    assert len(warmth) == 3
    assert warmth[0] > warmth[-1], "warmth should fall as the conversation is pushed"
    assert refusal[-1] > refusal[0], "refusal should rise"


def test_layer_0_is_off_unless_a_question_matches_a_corpus_prompt(corpus):
    """The load-bearing honesty of the whole view.

    An answer key is derived from a prompt written down in advance. A chat question has
    none, so Layer 0 cannot run — and the analysis says so per turn rather than emitting
    a confident number scored against the wrong key.
    """
    r = live.analyse("User: what is the airspeed of a swallow?\nAssistant: about 11 m/s.",
                     corpus)
    user = [t for t in r["turns"] if t["role"] == "user"][0]
    assert user["corpus_match"]["layer0"] is False
    assert not r["layer0_available_turns"]
    assert any("No Layer 0" in l for l in r["limits"])


def test_a_corpus_question_pasted_back_does_light_up_layer_0(corpus):
    """The bridge: if you asked something close to a corpus prompt, its key applies."""
    variant = corpus.all_variants[0]
    r = live.analyse(f"User: {variant.text}\nAssistant: here is an answer.", corpus)
    user = [t for t in r["turns"] if t["role"] == "user"][0]
    assert user["corpus_match"]["layer0"] is True
    assert user["corpus_match"]["prompt_id"] == variant.id


def test_limits_are_always_present():
    """A descriptive reading without its limits is the failure mode this instrument
    exists to avoid: the numbers look exactly like a campaign's, and nothing says
    otherwise."""
    r = live.analyse("Assistant: a bare answer with no markers.")
    assert r["limits"]
    assert any("Tier B" in l for l in r["limits"]), "provenance must always be stated"


def test_posture_is_unclassified_without_a_population():
    """Posture is relative to a campaign; with none, every turn is unclassified rather
    than forced into a box."""
    r = live.analyse("User: q\nAssistant: Let's work through this together.", corpus=None,
                     cuts=None)
    assert all(p == "unclassified" for p in r["posture_sequence"])
    assert any("No posture" in l for l in r["limits"])


def test_empty_input_is_handled():
    r = live.analyse("")
    assert r["n_turns"] == 0
    assert r["limits"]
