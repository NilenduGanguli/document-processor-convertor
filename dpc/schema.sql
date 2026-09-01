-- Conversion index. The markdown itself lives in S3 (pmd/{yyyy}/{mm}/{id}.md);
-- this table is the audit trail and the listing the console reads.
CREATE TABLE IF NOT EXISTS conversions (
    id              uuid PRIMARY KEY,
    doc_id          text,
    source          text,
    provider        text,
    filename        text,
    media_type      text,
    pages           int,
    blocks          int,
    tables_n        int,
    marks           int,
    key_values      int,
    chars           int,
    sha256_input    text,
    sha256_markdown text,
    s3_bucket       text,
    s3_key          text,
    status          text,
    error           text,
    ms              int,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversions_created_at_desc ON conversions (created_at DESC);

-- Doctree columns (SPEC-DOCTREE-1 §6.2). All nullable so every existing row stays valid and
-- every existing reader keeps working; ADD COLUMN IF NOT EXISTS keeps this file idempotent
-- under init_schema's apply-at-every-startup model (no migration machinery, same posture as
-- the table itself).
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_s3_key text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS sha256_tree text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_source text;      -- provider_sections|geometry|flat
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_nodes int;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_status text;      -- built|invalid:<rule>|error:<Type>
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_md_s3_key text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS sha256_tree_markdown text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS passes text;           -- canonical JSON of tree.passes

-- LLM arrangement artifacts (SPEC-DOCTREE-1 §4.6/§6.2): one row per pass over a conversion.
-- The artifact itself lives in S3 (arr/{yyyy}/{mm}/{id}.{n}.arr.json); the row is the index
-- plus the R17 variant-replay inputs (the exact `generated` string a variant was flattened
-- with, so variant replay is a pure function of recorded inputs).
CREATE TABLE IF NOT EXISTS arrangements (
    suggestion_id           uuid PRIMARY KEY,
    conversion_id           uuid NOT NULL REFERENCES conversions(id),
    artifact_sha256         text NOT NULL,
    s3_key                  text NOT NULL,
    status                  text NOT NULL,      -- ran|skipped:<reason>
    model_id                text,
    prompt_template_version text,
    verifier_version        text,
    n_accepted              int,
    n_rejected              int,
    variant_s3_key          text,
    variant_sha256          text,
    variant_generated       text,               -- R17
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS arrangements_conversion_created
    ON arrangements (conversion_id, created_at DESC);
