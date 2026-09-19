"""Safety Explorer — an instrument for measuring how model behaviour changes as
context framing becomes riskier while the underlying reasoning task stays similar.

See docs/PLAN.md for the experimental design and docs/DATA_INGESTION.md for how
observations enter the system.
"""

__version__ = "0.1.0"
CORPUS_VERSION = "0.1.0"
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
