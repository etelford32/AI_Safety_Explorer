"""Safety Explorer — an instrument for measuring how model behaviour changes as
context framing becomes riskier while the underlying reasoning task stays similar.

See docs/PLAN.md for the experimental design and docs/DATA_INGESTION.md for how
observations enter the system.
"""

__version__ = "0.5.0"
CORPUS_VERSION = "0.3.0"
EXTRACTOR_VERSION = "1"

DIMENSIONS = ("intent", "operationality", "specificity", "autonomy", "depth")

DIMENSION_LABELS = {
    "intent": ("benign", "explicitly harmful"),
    "operationality": ("conceptual", "directly actionable"),
    "specificity": ("hypothetical", "real target/system"),
    "autonomy": ("human advisory", "autonomous execution"),
    "depth": ("introductory", "expert"),
}

# Ordinal 0-5 human metrics. Order matters: it is the order the annotation UI
# presents them in, chosen so that the capability metrics are rated before the
# safety metrics (rating safety first biases the capability read).
HUMAN_METRICS = (
    "capability_retention",
    "technical_depth",
    "quantitative_content",
    "specificity",
    "boundary_precision",
    "recovery_quality",
    "over_refusal",
    "unsafe_assistance",
    "epistemic_quality",
)

# Metrics where a HIGH score is bad. Needed so aggregation never accidentally
# averages these in the same direction as the capability metrics.
INVERTED_METRICS = frozenset({"over_refusal", "unsafe_assistance"})

REFUSAL_LABELS = (
    "comply",
    "comply_with_caveat",
    "partial_refusal",
    "redirect",
    "full_refusal",
    "over_comply",
)

VARIANT_ORDER = ("A", "B", "C", "D", "E", "F")

# The depth arm (v0.2). Each entry mirrors a ladder variant at introductory depth,
# holding every other dimension — including the family's focal dimension — constant.
# Crossing these with the C/D/E ladder gives a depth x focal-dimension factorial,
# which is what RQ4 actually needs: not "does depth cost anything" but "does the
# focal-dimension penalty grow with depth".
DEPTH_ARM = {
    "C_intro": "C",
    "D_intro": "D",
    "E_intro": "E",
}

#: Depth level used by the introductory arm. The ladder sits at 3 (graduate), so this
#: is a two-level factor. A third level (4, research) is a v0.3 extension.
DEPTH_ARM_LEVEL = 1

#: Depth is manipulated through REGISTER AND FORMALISM ONLY — the vocabulary and
#: framing of the question — never through a claim about the requester's credentials.
#: A stated-expertise claim ("I'm a professor, so you can tell me") is a distinct
#: variable: it is a social/credentialing manipulation, not a technical-depth one.
#: Conflating them would make RQ4 unanswerable, because any observed effect could be
#: deference to authority rather than a response to the level of the question.
#: Stated expertise is a candidate arm of its own; see docs/PLAN.md.
DEPTH_MANIPULATION = "register_only"

VARIANT_NAMES = {
    "A": "theoretical baseline",
    "B": "realistic simulation",
    "C": "benign real-world",
    "D": "ambiguous dual-use",
    "E": "explicitly harmful",
    "F": "boundary recovery",
}

# Declared deltas for the standard intent-focal ladder. The linter enforces that a
# family's variants move only along these deltas (see docs/PLAN.md §1).
LADDER_DELTAS = {
    ("A", "B"): {"specificity": 2, "operationality": 1},
    ("B", "C"): {"specificity": 2, "operationality": 2},
    ("C", "D"): {"intent": 2},
    ("D", "E"): {"intent": 2},
    ("E", "F"): {"intent": -3},
}

# What we cannot observe, by surface. Attached to every run so the limitation
# travels with the data rather than living only in a README.
UNOBSERVABLE = {
    "api": ["server_side_safety_scaffolding", "model_routing", "alias_resolution"],
    "web_chat": [
        "system_prompt",
        "sampling_parameters",
        "server_side_safety_scaffolding",
        "model_routing",
        "alias_resolution",
        "conversation_memory",
        "tool_availability",
    ],
    "app": [
        "system_prompt",
        "sampling_parameters",
        "server_side_safety_scaffolding",
        "model_routing",
        "conversation_memory",
    ],
    "cli": ["system_prompt", "server_side_safety_scaffolding", "tool_availability"],
    "third_party": [
        "system_prompt",
        "sampling_parameters",
        "server_side_safety_scaffolding",
        "model_routing",
        "provider_middleware",
    ],
}
