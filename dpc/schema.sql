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
