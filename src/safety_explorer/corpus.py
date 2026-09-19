"""Corpus loading.

Prompts live in version-controlled TOML, not in the database. The database holds a
*snapshot* of a corpus version, content-addressed, so that a run can always be traced
back to the exact prompt text that produced it even after the corpus moves on.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import CORPUS_VERSION, DIMENSIONS

CORPUS_ROOT = Path("corpus")


def normalise(text: str) -> str:
    """Whitespace-normalised text, used for hashing and for matching imports."""
    return re.sub(r"\s+", " ", text).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])")


def numeric_signature(text: str) -> frozenset[str]:
    """The set of numeric literals in a prompt, normalised.

    Used to verify that two prompts pose the *same physical problem*. This is the
    guarantee that replaces vocabulary matching for depth twins: an introductory and a
    graduate phrasing of one question must differ in wording — that is the manipulation
    — but they must not differ in a single parameter, or they are different questions.
    """
    out = set()
    for tok in _NUMBER_RE.findall(text):
        tok = tok.replace(",", "")
        try:
            out.add(repr(float(tok)))
        except ValueError:
            continue
    return frozenset(out)


@dataclass
class Variant:
    id: str
    variant: str
    title: str
    text: str
    output_format: str
    intent: int
    operationality: int
    specificity: int
    autonomy: int
    depth: int
    hazard_review: str
    hazard_rationale: str
    expected_benign: bool
    status: str = "active"
    arm: str = "family"
    sub_arm: str = "ladder"
    control_arm: str | None = None
    family_id: str | None = None
    twin_group_id: str | None = None
    baseline: str | None = None
    conversation_with: str | None = None

    @property
    def vector(self) -> dict[str, int]:
        return {d: getattr(self, d) for d in DIMENSIONS}

    @property
    def prompt_hash(self) -> str:
        return text_hash(self.text)

    @property
    def numeric_signature(self) -> frozenset[str]:
        return numeric_signature(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class TwinGroup:
    id: str
    family_id: str
    reasoning_core: str
    output_format: str
    baseline_variant: str = "A"


@dataclass
class Family:
    id: str
    name: str
    domain: str
    focal_dimension: str
    reasoning_core: str
    substrate_rule: str
    status: str = "active"
    ladder: dict[str, dict[str, int]] = field(default_factory=dict)
    twin_groups: list[TwinGroup] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def ladder_variants(self) -> list["Variant"]:
        return [v for v in self.variants if v.sub_arm == "ladder"]

    @property
    def depth_arm(self) -> list["Variant"]:
        return [v for v in self.variants if v.sub_arm == "depth"]


@dataclass
class Corpus:
    version: str
    families: list[Family] = field(default_factory=list)
    controls: list[Variant] = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)
    source_files: list[Path] = field(default_factory=list)

    @property
    def all_variants(self) -> list[Variant]:
        out: list[Variant] = []
        for fam in self.families:
            out.extend(fam.variants)
        out.extend(self.controls)
        return out

    @property
    def runnable(self) -> list[Variant]:
        """Variants a campaign may actually execute.

        Stubs and anything without a clear hazard review are excluded here rather
        than at run time, so an unfinished prompt cannot reach a model by accident.
        """
        return [
            v for v in self.all_variants
            if v.status == "active" and v.hazard_review == "clear"
        ]

    def by_id(self, vid: str) -> Variant | None:
        for v in self.all_variants:
            if v.id == vid:
                return v
        return None

    def family(self, fid: str) -> Family | None:
        return next((f for f in self.families if f.id == fid), None)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for v in sorted(self.all_variants, key=lambda x: x.id):
            h.update(v.id.encode())
            h.update(v.prompt_hash.encode())
            h.update(str(sorted(v.vector.items())).encode())
        return h.hexdigest()


def load_family(path: Path) -> Family:
    data = tomllib.loads(path.read_text())
    f = data["family"]
    ladder_raw = f.get("ladder", {})
    ladder = {k: dict(v) for k, v in ladder_raw.items()}

    fam = Family(
        id=f["id"],
        name=f["name"],
        domain=f["domain"],
        focal_dimension=f["focal_dimension"],
        reasoning_core=f["reasoning_core"].strip(),
        substrate_rule=f["substrate_rule"],
        status=f.get("status", "active"),
        ladder=ladder,
    )

    groups: dict[str, TwinGroup] = {}
    for g in data.get("twin_group", []):
        tg = TwinGroup(
            id=g["id"],
            family_id=fam.id,
            reasoning_core=g["reasoning_core"].strip(),
            output_format=g["output_format"],
            baseline_variant=g.get("baseline_variant", "A"),
        )
        groups[tg.id] = tg
        fam.twin_groups.append(tg)

    for v in data.get("variant", []):
        tg_id = v["twin_group"]
        group = groups.get(tg_id)
        fam.variants.append(
            Variant(
                id=v["id"],
                variant=v["variant"],
                title=v["title"],
                text=v["text"].strip(),
                output_format=v.get("output_format") or (group.output_format if group else ""),
                intent=v["intent"],
                operationality=v["operationality"],
                specificity=v["specificity"],
                autonomy=v["autonomy"],
                depth=v["depth"],
                hazard_review=v["hazard_review"],
                hazard_rationale=v["hazard_rationale"],
                expected_benign=bool(v.get("expected_benign", True)),
                status=v.get("status", fam.status),
                arm="family",
                sub_arm=v.get("sub_arm", "ladder"),
                family_id=fam.id,
                twin_group_id=tg_id,
                baseline=v.get("baseline") or None,
                conversation_with=v.get("conversation_with") or None,
            )
        )
    return fam


def load_controls(path: Path) -> list[Variant]:
    data = tomllib.loads(path.read_text())
    out = []
    for c in data.get("control", []):
        out.append(
            Variant(
                id=c["id"],
                variant=c["id"].split(".", 1)[-1],
                title=c["title"],
                text=c["text"].strip(),
                output_format=c["output_format"],
                intent=c["intent"],
                operationality=c["operationality"],
                specificity=c["specificity"],
                autonomy=c["autonomy"],
                depth=c["depth"],
                hazard_review=c["hazard_review"],
                hazard_rationale=c["hazard_rationale"],
                expected_benign=bool(c.get("expected_benign", True)),
                status=c.get("status", "active"),
                arm="control",
                sub_arm="control",
                control_arm=c["arm"],
            )
        )
    return out


def load(root: Path = CORPUS_ROOT) -> Corpus:
    root = Path(root)
    dims = tomllib.loads((root / "dimensions.toml").read_text())
    corpus = Corpus(version=dims.get("corpus_version", CORPUS_VERSION), dimensions=dims)

    for p in sorted((root / "families").glob("*.toml")):
        corpus.families.append(load_family(p))
        corpus.source_files.append(p)

    for p in sorted((root / "controls").glob("*.toml")):
        corpus.controls.extend(load_controls(p))
        corpus.source_files.append(p)

    return corpus
