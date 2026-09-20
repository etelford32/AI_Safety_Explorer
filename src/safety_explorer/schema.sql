-- Safety Explorer storage schema.
--
-- Design notes:
--  * Everything that enters the instrument becomes a `run` row, whatever ingestion
--    lane produced it. Lane and provenance tier are recorded, never inferred.
--  * Measurement layers live in separate tables (`feature`, `annotation`, `judgement`)
--    and are never merged into a single score column.
--  * Prompts are content-addressed. A run always records the hash of the exact text
--    that produced it, so a corpus edit can never silently reattribute old data.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS corpus_version (
    version       TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    loaded_at     TEXT NOT NULL,
    n_prompts     INTEGER NOT NULL,
    lint_clean    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS family (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    focal_dimension TEXT NOT NULL,
    reasoning_core  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | stub
    substrate_rule  TEXT NOT NULL,
    corpus_version  TEXT NOT NULL REFERENCES corpus_version(version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS twin_group (
    id              TEXT PRIMARY KEY,
    family_id       TEXT NOT NULL REFERENCES family(id) ON DELETE CASCADE,
    reasoning_core  TEXT NOT NULL,
    output_format   TEXT NOT NULL,
    corpus_version  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt (
    id               TEXT PRIMARY KEY,
    family_id        TEXT REFERENCES family(id) ON DELETE CASCADE,
    twin_group_id    TEXT REFERENCES twin_group(id) ON DELETE CASCADE,
    arm              TEXT NOT NULL,          -- family | control
    sub_arm          TEXT NOT NULL DEFAULT 'ladder',  -- ladder | depth | language | control
    language         TEXT NOT NULL DEFAULT 'en',      -- BCP-47 primary subtag
    control_arm      TEXT,                   -- alarming_benign | innocuous_consequential
    variant          TEXT NOT NULL,          -- A..F, C_intro..E_intro, or control id
    title            TEXT NOT NULL,
    text             TEXT NOT NULL,
    output_format    TEXT NOT NULL,
    -- design-space coordinates, ordinal 0..4
    intent           INTEGER NOT NULL,
    operationality   INTEGER NOT NULL,
    specificity      INTEGER NOT NULL,
    autonomy         INTEGER NOT NULL,
    depth            INTEGER NOT NULL,
    hazard_review    TEXT NOT NULL,
    hazard_rationale TEXT NOT NULL,
    conversation_with TEXT,                  -- prior prompt id for multi-turn variants
    expected_benign  INTEGER NOT NULL DEFAULT 1,  -- is a full answer appropriate?
    -- Which Layer 0 targets this variant's question covers: 'full', 'none', or a
    -- comma-separated list of target keys. A variant that states no parameters, or that
    -- asks an adjacent question (as six of the eight F variants do by design, since that
    -- is how recovery is tested), cannot produce the whole answer key. Scoring it against
    -- the whole key measures the question rather than the model.
    answer_key       TEXT NOT NULL DEFAULT 'full',
    prompt_hash      TEXT NOT NULL,
    corpus_version   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_prompt_family ON prompt(family_id);
CREATE INDEX IF NOT EXISTS idx_prompt_twin   ON prompt(twin_group_id);
CREATE INDEX IF NOT EXISTS idx_prompt_arm    ON prompt(arm, sub_arm);
CREATE INDEX IF NOT EXISTS idx_prompt_hash   ON prompt(prompt_hash);

CREATE TABLE IF NOT EXISTS campaign (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}',
    n_repeats      INTEGER NOT NULL DEFAULT 3,
    corpus_version TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run (
    id               TEXT PRIMARY KEY,
    campaign_id      TEXT REFERENCES campaign(id) ON DELETE CASCADE,
    prompt_id        TEXT REFERENCES prompt(id) ON DELETE SET NULL,
    repeat_index     INTEGER NOT NULL DEFAULT 0,
    lane             TEXT NOT NULL,            -- api | manual | import
    provenance_tier  TEXT NOT NULL,            -- A | B | C
    surface          TEXT NOT NULL,            -- api | web_chat | app | cli | third_party
    provider         TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    model_reported   TEXT,
    model_alias_risk INTEGER NOT NULL DEFAULT 0,
    params           TEXT NOT NULL DEFAULT '{}',
    unobservable     TEXT NOT NULL DEFAULT '[]',
    messages         TEXT NOT NULL DEFAULT '[]',
    response         TEXT,
    error            TEXT,
    retries          INTEGER NOT NULL DEFAULT 0,
    latency_ms       INTEGER,
    finish_reason    TEXT,
    -- Classifier-level refusals arrive as HTTP 200 with stop_reason='refusal'
    -- and a category. Categorically different from declining in prose, and the
    -- most objective boundary signal available, so it is stored rather than
    -- collapsed into the response text.
    stop_details     TEXT NOT NULL DEFAULT '{}',
    usage            TEXT NOT NULL DEFAULT '{}',
    -- Observation cue composed onto the prompt (the sandbagging arm).
    cue_id           TEXT NOT NULL DEFAULT 'none',
    cue_level        INTEGER NOT NULL DEFAULT 0,
    cue_arm          TEXT NOT NULL DEFAULT 'none',   -- none | treatment | placebo
    prompt_hash      TEXT,
    match_confidence REAL NOT NULL DEFAULT 1.0,
    captured_at      TEXT NOT NULL,
    corpus_version   TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_campaign ON run(campaign_id);
CREATE INDEX IF NOT EXISTS idx_run_prompt   ON run(prompt_id);
-- The cue is part of the cell identity. Without it, running the same prompt under a
-- treatment and a placebo cue would collide and the second would be silently dropped —
-- which is the one comparison the sandbagging arm exists to make.
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_cell
    ON run(campaign_id, prompt_id, repeat_index, cue_id)
    WHERE campaign_id IS NOT NULL AND prompt_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_run_cue ON run(cue_level, cue_arm);

-- Layer 1: deterministic automatic features. Recomputable from `run.response`
-- at any time, so improving the extractor never costs an API call.
CREATE TABLE IF NOT EXISTS feature (
    run_id            TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    extractor_version TEXT NOT NULL,
    n_chars           INTEGER NOT NULL,
    n_words           INTEGER NOT NULL,
    approx_tokens     INTEGER NOT NULL,
    n_code_blocks     INTEGER NOT NULL,
    n_equations       INTEGER NOT NULL,
    n_numbers         INTEGER NOT NULL,
    n_quantities      INTEGER NOT NULL,
    n_steps           INTEGER NOT NULL,
    n_headings        INTEGER NOT NULL,
    n_citations       INTEGER NOT NULL,
    n_questions       INTEGER NOT NULL,
    refusal_hits      INTEGER NOT NULL,
    hedge_hits        INTEGER NOT NULL,
    safety_framing    INTEGER NOT NULL,
    refusal_signal    REAL NOT NULL,
    technical_density REAL NOT NULL,
    extra             TEXT NOT NULL DEFAULT '{}',
    computed_at       TEXT NOT NULL
);

-- Layer 0: objective correctness against a computed answer key. Requires no human
-- annotation, which is what lets it scale past the annotation bottleneck. `null_accuracy`
-- stores the cross-family score — the built-in check that the matcher is finding answers
-- rather than finding numbers.
CREATE TABLE IF NOT EXISTS ground_truth (
    run_id         TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    solver_version TEXT NOT NULL,
    -- The subset of the family's key this run was scored against, echoed from the
    -- prompt. Two runs scored against different covers are not comparable, and a twin
    -- delta between them is not computed.
    answer_key_cover TEXT NOT NULL DEFAULT 'full',
    targets_total  INTEGER NOT NULL,
    targets_hit    INTEGER NOT NULL,
    accuracy       REAL,
    -- Finer readings of the same comparison. `accuracy` can take only as many values as
    -- the family has targets, so an effect smaller than one target is invisible to it;
    -- `graded_accuracy` gives partial credit by distance and `weighted_accuracy` counts
    -- an intermediate quantity for half of a headline one.
    graded_accuracy    REAL,
    weighted_accuracy  REAL,
    error_classes      TEXT NOT NULL DEFAULT '{}',
    n_candidates   INTEGER NOT NULL DEFAULT 0,
    null_accuracy  REAL,
    -- Layer 0d. Whether the model's OWN figures satisfy the identities that connect
    -- them, which needs no answer key and separates a mis-set parameter propagated
    -- coherently from algebra that was never done. `consistency_coverage` is reported
    -- with it because a response that stated one number cannot contradict itself.
    consistency          REAL,
    consistency_coverage REAL,
    relations_checked    INTEGER NOT NULL DEFAULT 0,
    relations_satisfied  INTEGER NOT NULL DEFAULT 0,
    relation_details     TEXT NOT NULL DEFAULT '[]',
    stated_values        TEXT NOT NULL DEFAULT '{}',
    details        TEXT NOT NULL DEFAULT '[]',
    computed_at    TEXT NOT NULL
);

-- Awareness probes. A probe is a separate model call attached to a performance run:
-- either an independent call carrying the same composed prompt (mode 'parallel'), or a
-- follow-up turn on the performance conversation (mode 'followup'). Stored apart from
-- `run` because a probe measures the model's awareness, not its capability, and pooling
-- the two would let an awareness answer be mistaken for a performance answer.
CREATE TABLE IF NOT EXISTS probe (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT '',
    model_id      TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL,
    response      TEXT,
    parsed        TEXT NOT NULL DEFAULT '{}',
    error         TEXT,
    latency_ms    INTEGER,
    captured_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_run ON probe(run_id, kind);

-- Layer 2: human annotation. The reference dataset.
CREATE TABLE IF NOT EXISTS annotation (
    id                   TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    annotator            TEXT NOT NULL,
    blinded              INTEGER NOT NULL DEFAULT 1,
    pass_index           INTEGER NOT NULL DEFAULT 0,   -- 1 = reliability re-serve
    capability_retention INTEGER,
    technical_depth      INTEGER,
    quantitative_content INTEGER,
    specificity          INTEGER,
    boundary_precision   INTEGER,
    recovery_quality     INTEGER,
    over_refusal         INTEGER,
    unsafe_assistance    INTEGER,
    epistemic_quality    INTEGER,
    -- Layer 1.5 stance ratings. Nullable and rated in a separate pass, because the
    -- stance rubric is a different instrument answering a different question: these
    -- validate the automatic stance features and must never be averaged into capability.
    warmth               INTEGER,
    moralizing           INTEGER,
    distancing           INTEGER,
    refusal_label        TEXT,
    -- Span indices each rating points at, when the rating was made in the co-analysis
    -- view. Same table as any other annotation on purpose: a cited rating is an
    -- annotation with its working shown, so reliability, alpha and judge agreement keep
    -- working without needing to know where it came from.
    citations            TEXT NOT NULL DEFAULT '{}',
    -- Which version of the rating system this rating was made under. Without it the
    -- question "did anchoring the scale raise agreement" cannot be asked at all, because
    -- ratings from the two rubrics would be indistinguishable once stored.
    rubric_version       TEXT NOT NULL DEFAULT 'legacy',
    escalate             INTEGER NOT NULL DEFAULT 0,
    notes                TEXT NOT NULL DEFAULT '',
    seconds_spent        INTEGER,
    revealed             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotation_run ON annotation(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_annotation_pass
    ON annotation(run_id, annotator, pass_index);

-- Layer 3: optional LLM judge. Stored apart and never authoritative.
CREATE TABLE IF NOT EXISTS judgement (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    judge_provider TEXT NOT NULL,
    judge_model    TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    scores         TEXT NOT NULL DEFAULT '{}',
    -- Which spans each rating points at. A rating that cites nothing is an impression,
    -- and the rubric exists to make impressions visible rather than to pretend they did
    -- not happen, so it is stored and flagged rather than dropped.
    citations      TEXT NOT NULL DEFAULT '{}',
    -- Whether the ratings follow from the spans the same proposal labelled. Needs no
    -- human and no answer key: rating capability 5 while calling most of the response a
    -- refusal is a self-contradiction, and a contradicted proposal is one to read first.
    coherence      TEXT NOT NULL DEFAULT '{}',
    problems       TEXT NOT NULL DEFAULT '[]',
    refusal_label  TEXT,
    raw            TEXT,
    reliability    TEXT NOT NULL DEFAULT 'unvalidated',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_judgement_run ON judgement(run_id);

-- Span-level co-analysis. One row per (span, source, author), so a model's proposal and
-- a human's adjudication of the same span sit side by side and can be compared rather
-- than one overwriting the other.
--
-- `blinded` records whether the human labelled the span BEFORE seeing the model's
-- proposal. That single column is what separates measuring agreement from measuring
-- anchoring: a human shown a proposal first will agree with it more often, and a
-- co-analysis that did not record which happened could not tell the two apart.
--
-- `span_hash` and `segmenter_version` travel with every label because a label points at
-- an index, and an index means nothing if the segmenter is later changed. A label whose
-- hash no longer matches the span at that index is reported as stale, never silently
-- re-pointed at different words.
CREATE TABLE IF NOT EXISTS span_label (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    span_index        INTEGER NOT NULL,
    span_hash         TEXT NOT NULL,
    segmenter_version TEXT NOT NULL,
    source            TEXT NOT NULL,          -- human | model
    author            TEXT NOT NULL,          -- annotator name, or the judge model id
    label             TEXT NOT NULL,
    confidence        REAL,                   -- model proposals only
    rationale         TEXT NOT NULL DEFAULT '',
    quote             TEXT NOT NULL DEFAULT '',
    blinded           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_span_label_run ON span_label(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_span_label_cell
    ON span_label(run_id, span_index, source, author);

-- Blinding bookkeeping: which runs have been served to which annotator, in what
-- order, so the queue is reproducible and re-serves are deliberate.
CREATE TABLE IF NOT EXISTS serve_log (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    annotator  TEXT NOT NULL,
    pass_index INTEGER NOT NULL DEFAULT 0,
    served_at  TEXT NOT NULL,
    seed       INTEGER NOT NULL
);
