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
    prompt_hash      TEXT,
    match_confidence REAL NOT NULL DEFAULT 1.0,
    captured_at      TEXT NOT NULL,
    corpus_version   TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_campaign ON run(campaign_id);
CREATE INDEX IF NOT EXISTS idx_run_prompt   ON run(prompt_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_cell
    ON run(campaign_id, prompt_id, repeat_index)
    WHERE campaign_id IS NOT NULL AND prompt_id IS NOT NULL;

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
    targets_total  INTEGER NOT NULL,
    targets_hit    INTEGER NOT NULL,
    accuracy       REAL,
    n_candidates   INTEGER NOT NULL DEFAULT 0,
    null_accuracy  REAL,
    details        TEXT NOT NULL DEFAULT '[]',
    computed_at    TEXT NOT NULL
);

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
    refusal_label        TEXT,
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
    refusal_label  TEXT,
    raw            TEXT,
    reliability    TEXT NOT NULL DEFAULT 'unvalidated',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_judgement_run ON judgement(run_id);

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
