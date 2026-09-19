from safety_explorer import metrics


def test_refusal_is_detected():
    f = metrics.extract("I'm sorry, I can't help with that request.")
    assert f["refusal_hits"] >= 1
    assert f["refusal_signal"] > 0


def test_technical_content_is_counted():
    text = (
        "The condition is $N_f > 1$.\n\n"
        "1. Sweep rate is 7.6 km/s over 10 m^2.\n"
        "2. Density is about 2400 objects.\n\n"
        "See Kessler & Cour-Palais (1978)."
    )
    f = metrics.extract(text)
    assert f["n_equations"] >= 1
    assert f["n_quantities"] >= 2
    assert f["n_steps"] >= 2
    assert f["n_citations"] >= 1


def test_density_is_not_capped():
    """A cap would flatten the ratio that the twin comparison depends on."""
    dense = "$a=1$ $b=2$ $c=3$ $d=4$ 5 km 6 kg 7 m/s"
    assert metrics.extract(dense)["technical_density"] > 1.0


def test_empty_response_is_not_an_error():
    f = metrics.extract(None)
    assert f["n_words"] == 0 and f["extra"]["empty"] is True


def test_retention_handles_a_total_loss():
    """A refusal has zero density; that must be a 0.0 ratio, never missing data."""
    base = metrics.extract("$E=mc^2$ and 10 km and 5 kg")
    test = metrics.extract("I can't help with that.")
    r = metrics.retention(test, base)
    assert r["equation_ratio"] == 0.0
    assert r["quantity_ratio"] == 0.0


def test_word_diff_marks_deletions():
    diff = metrics.word_diff("the answer is 42 exactly", "the answer is withheld")
    assert ("delete", "42 exactly") in diff or any(op == "delete" for op, _ in diff)
