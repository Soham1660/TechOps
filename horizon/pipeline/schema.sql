-- The horizon schema: git blame, as tables.
--
-- A new schema in the existing database. The legacy gitea_analytics tables stay
-- untouched, because they are the baseline the new numbers get read against --
-- a ranking disagreement between the two means this pipeline is wrong.
--
-- Every table is run-scoped. Blame is a snapshot of now, so a rerun is a new
-- answer rather than a correction of the old one; without run_id the second run
-- destroys the evidence for the first.
--
-- Applied by PostgresResource.ensure_schema() at the start of every load, which
-- is why every statement is IF NOT EXISTS. Later PRs append their feature tables
-- to this file.

CREATE SCHEMA IF NOT EXISTS horizon;

CREATE TABLE IF NOT EXISTS horizon.runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    gitea_url   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running|complete|partial|failed
    warnings    JSONB NOT NULL DEFAULT '[]'::JSONB
);

CREATE TABLE IF NOT EXISTS horizon.repositories (
    run_id         TEXT NOT NULL REFERENCES horizon.runs(run_id) ON DELETE CASCADE,
    organization   TEXT NOT NULL,
    repository     TEXT NOT NULL,
    default_branch TEXT,
    ssh_url        TEXT,
    html_url       TEXT,
    head_sha       TEXT,
    blamed_files   INTEGER NOT NULL DEFAULT 0,
    skipped_files  INTEGER NOT NULL DEFAULT 0,
    blame_status   TEXT NOT NULL DEFAULT 'pending', -- pending|complete|partial|failed|skipped
    PRIMARY KEY (run_id, organization, repository)
);

-- One row per commit with surviving lines; per-commit blame fields live here
-- rather than repeating on every attribution row.
CREATE TABLE IF NOT EXISTS horizon.blame_commits (
    run_id          TEXT NOT NULL,
    organization    TEXT NOT NULL,
    repository      TEXT NOT NULL,
    commit_sha      TEXT NOT NULL,
    author_name     TEXT NOT NULL,
    author_email    TEXT NOT NULL,
    author_time     TIMESTAMPTZ,
    author_tz       TEXT,
    committer_name  TEXT,
    committer_email TEXT,
    committer_time  TIMESTAMPTZ,
    summary         TEXT,
    is_boundary     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, organization, repository, commit_sha),
    FOREIGN KEY (run_id, organization, repository)
        REFERENCES horizon.repositories(run_id, organization, repository) ON DELETE CASCADE
);

-- Surviving line counts per file per commit. source_path is blame's `filename`,
-- which differs from path under -M/-C when the line originated elsewhere, so it
-- belongs in the key. moved_from is blame's `previous`.
CREATE TABLE IF NOT EXISTS horizon.blame_attribution (
    run_id       TEXT NOT NULL,
    organization TEXT NOT NULL,
    repository   TEXT NOT NULL,
    path         TEXT NOT NULL,
    commit_sha   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    moved_from   TEXT,
    lines        INTEGER NOT NULL CHECK (lines > 0),
    PRIMARY KEY (run_id, organization, repository, path, commit_sha, source_path),
    FOREIGN KEY (run_id, organization, repository, commit_sha)
        REFERENCES horizon.blame_commits(run_id, organization, repository, commit_sha)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS blame_commits_author_idx
    ON horizon.blame_commits (author_email, run_id);
CREATE INDEX IF NOT EXISTS blame_attribution_path_idx
    ON horizon.blame_attribution (run_id, organization, repository, path);
