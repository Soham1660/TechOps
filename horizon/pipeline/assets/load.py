"""L4 load: records into Postgres. The only layer that writes.

Two tables, because the porcelain repeats a commit's metadata on every line it
touched. `blame_commits` holds each commit once; `blame_attribution` holds the
surviving line counts per file. Storing them together would repeat an author's
name and email on all 36,653 rows of a single repository.

Both assets write inside one transaction each, so a load that fails partway
leaves nothing behind. A half-written run that reads as a complete one is the
failure this pipeline exists to avoid.
"""

# No `from __future__ import annotations` in this module: Dagster resolves the
# context and asset-input annotations at decoration time, and PEP 563 turns them
# into strings it then refuses.
from dataclasses import dataclass
from datetime import datetime, timezone

from dagster import AssetExecutionContext, MetadataValue, asset

from ..blame_parser import BlameRecord
from ..metadata import table_schema_from
from ..resources import GiteaClient, PostgresResource
from .capture import BlameCapture


@dataclass(frozen=True)
class LoadResult:
    """What a load wrote, so the next asset and the checks need not re-query."""

    table: str
    run_id: str
    organization: str
    repository: str
    rows_written: int
    lines_written: int


LOAD_RESULT_SCHEMA = table_schema_from(
    LoadResult,
    {
        "table": ("string", "Table the rows landed in."),
        "run_id": ("string", "Dagster run this snapshot belongs to."),
        "organization": ("string", "Gitea organization loaded."),
        "repository": ("string", "Repository loaded."),
        "rows_written": ("int", "Rows inserted by this asset."),
        "lines_written": ("int", "Blamed lines those rows account for."),
    },
)


@asset(
    group_name="l4_load",
    kinds={"postgres"},
    description=(
        "One row per commit with surviving lines. Per-commit blame fields live "
        "here rather than repeating on every attribution row."
    ),
    metadata={
        "dagster/column_schema": LOAD_RESULT_SCHEMA,
        "table": "horizon.blame_commits",
        "grain": "one row per run, repository and commit",
    },
)
def blame_commits_table(
    context: AssetExecutionContext,
    blame_records: list[BlameRecord],
    blame_capture: BlameCapture,
    postgres: PostgresResource,
    gitea: GiteaClient,
) -> LoadResult:
    postgres.ensure_schema()
    run_id = context.run.run_id

    commits: dict[str, BlameRecord] = {}
    boundary: set[str] = set()
    for record in blame_records:
        commits.setdefault(record.commit_sha, record)
        if record.reaches_history_boundary:
            boundary.add(record.commit_sha)

    rows = [
        (
            run_id,
            blame_capture.org,
            blame_capture.repo,
            sha,
            record.author_name,
            record.author_email,
            _as_timestamp(record.authored_at_epoch),
            record.author_utc_offset,
            record.committer_name,
            record.committer_email,
            _as_timestamp(record.committed_at_epoch),
            record.commit_summary,
            sha in boundary,
        )
        for sha, record in commits.items()
    ]

    with postgres.connect() as connection:
        _write_run_and_repository(connection, run_id, blame_capture, gitea.base_url)
        connection.cursor().executemany(
            """
            INSERT INTO horizon.blame_commits (
                run_id, organization, repository, commit_sha,
                author_name, author_email, author_time, author_tz,
                committer_name, committer_email, committer_time,
                summary, is_boundary
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, organization, repository, commit_sha) DO NOTHING
            """,
            rows,
        )

    result = LoadResult(
        table="horizon.blame_commits",
        run_id=run_id,
        organization=blame_capture.org,
        repository=blame_capture.repo,
        rows_written=len(rows),
        lines_written=len(blame_records),
    )
    context.add_output_metadata(_as_metadata(result, boundary_commits=len(boundary)))
    return result


@asset(
    group_name="l4_load",
    kinds={"postgres"},
    description=(
        "Surviving line counts per file per commit. source_path is blame's "
        "`filename`, which differs from path under -M/-C, so it is part of the key."
    ),
    metadata={
        "dagster/column_schema": LOAD_RESULT_SCHEMA,
        "table": "horizon.blame_attribution",
        "grain": "one row per run, repository, path, commit and source path",
    },
)
def blame_attribution_table(
    context: AssetExecutionContext,
    blame_records: list[BlameRecord],
    blame_commits_table: LoadResult,
    postgres: PostgresResource,
) -> LoadResult:
    """Depends on blame_commits_table for the foreign key, not for its value.

    Attribution rows reference commits, so the commits have to be in the table
    before these rows can land. Taking the upstream result as an argument is what
    makes Dagster guarantee that ordering.
    """

    run_id = blame_commits_table.run_id
    counts: dict[tuple[str, str, str], int] = {}
    moved_from: dict[tuple[str, str, str], str | None] = {}

    for record in blame_records:
        key = (record.blamed_file_path, record.commit_sha, record.origin_file_path)
        counts[key] = counts.get(key, 0) + 1
        # `previous` is per line, so a group can carry more than one. The first
        # non-null is kept rather than the last, so a rerun writes the same row.
        if moved_from.get(key) is None:
            moved_from[key] = record.previous_commit_sha

    rows = [
        (
            run_id,
            blame_commits_table.organization,
            blame_commits_table.repository,
            path,
            sha,
            source_path,
            moved_from[(path, sha, source_path)],
            lines,
        )
        for (path, sha, source_path), lines in counts.items()
    ]

    with postgres.connect() as connection:
        connection.cursor().executemany(
            """
            INSERT INTO horizon.blame_attribution (
                run_id, organization, repository, path, commit_sha,
                source_path, moved_from, lines
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, organization, repository, path, commit_sha, source_path)
            DO NOTHING
            """,
            rows,
        )

    result = LoadResult(
        table="horizon.blame_attribution",
        run_id=run_id,
        organization=blame_commits_table.organization,
        repository=blame_commits_table.repository,
        rows_written=len(rows),
        lines_written=sum(counts.values()),
    )
    context.add_output_metadata(
        _as_metadata(result, moved_rows=sum(1 for value in moved_from.values() if value))
    )
    return result


def _write_run_and_repository(
    connection,
    run_id: str,
    capture: BlameCapture,
    gitea_url: str | None,
) -> None:
    """The two parent rows every blame row hangs off.

    Written by the first load asset rather than by an asset of their own: they
    are bookkeeping about the run, not data blame produced, and an asset that
    only writes its own provenance is noise in the graph.
    """

    connection.execute(
        """
        INSERT INTO horizon.runs (run_id, started_at, gitea_url, status)
        VALUES (%s, %s, %s, 'running')
        ON CONFLICT (run_id) DO NOTHING
        """,
        (run_id, datetime.now(timezone.utc), gitea_url or "unknown"),
    )
    connection.execute(
        """
        INSERT INTO horizon.repositories (
            run_id, organization, repository, default_branch,
            blamed_files, skipped_files, blame_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, organization, repository) DO NOTHING
        """,
        (
            run_id,
            capture.org,
            capture.repo,
            capture.default_branch,
            capture.files_captured,
            capture.files_skipped_binary + capture.files_failed,
            "partial" if capture.files_failed else "complete",
        ),
    )


def _as_metadata(result: LoadResult, **extra: int) -> dict:
    return {
        "table": MetadataValue.text(result.table),
        "repository": MetadataValue.text(f"{result.organization}/{result.repository}"),
        "rows_written": MetadataValue.int(result.rows_written),
        "lines_written": MetadataValue.int(result.lines_written),
        **{name: MetadataValue.int(value) for name, value in extra.items()},
    }


def _as_timestamp(epoch: int | None) -> datetime | None:
    """Epoch seconds are absolute, so UTC is the honest storage.

    The author's own offset is kept separately in author_tz -- it says what time
    it was where they were, which is a fact about the person, not the instant.
    """

    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch is not None else None
