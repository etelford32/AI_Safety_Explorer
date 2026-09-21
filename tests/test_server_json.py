"""The JSON the server sends must be valid JSON — no NaN, no Infinity.

json.dumps emits `NaN`/`Infinity` by default for the non-finite floats a bootstrap CI
returns when it cannot estimate; those are not valid JSON and the browser's JSON.parse
rejects them, taking down whatever view fetched them. `_json_safe` maps them to null.
"""

from __future__ import annotations

import json
import math

from safety_explorer.server import _json_safe


def test_non_finite_floats_become_null():
    out = _json_safe({"ci95": [float("nan"), 1.0],
                      "nested": [{"a": float("inf")}, {"b": -math.inf}],
                      "ok": 0.5, "s": "text", "n": 3})
    assert out["ci95"] == [None, 1.0]
    assert out["nested"] == [{"a": None}, {"b": None}]
    assert out["ok"] == 0.5 and out["s"] == "text" and out["n"] == 3


def test_the_result_is_strict_json_parseable():
    payload = {"x": float("nan"), "y": [float("inf"), 2.0]}
    # allow_nan=False would raise on the raw payload; after _json_safe it must not.
    text = json.dumps(_json_safe(payload), allow_nan=False)
    assert json.loads(text) == {"x": None, "y": [None, 2.0]}
