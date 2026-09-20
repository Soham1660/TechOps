"""Asset checks: the places where "it ran" is not the same as "it worked".

Every check here exists because the legacy run passed without one. It recorded
``blame_lines: 0`` for all 132 members and reported ``blame_status: complete``,
because nothing threw -- so completeness was inferred from the absence of an
exception rather than from the presence of data. These assert the presence of
data.
"""

# No `from __future__ import annotations` in this module: Dagster resolves the
# context and asset-input annotations at decoration time, and PEP 563 turns them
# into strings it then refuses.
from dagster import AssetCheckResult, AssetCheckSeverity, AssetIn, asset_check

from .assets import (
    BlameCapture,
    LoadResult,
    blame_attribution_table,
    blame_capture,
    blame_records,
)
from .blame_parser import BlameRecord
from .resources import PostgresResource


@asset_check(
    asset=blame_capture,
    description="A capture that blamed no files at all is a failure, not an empty repo.",
)
def capture_is_non_empty(blame_capture: BlameCapture) -> AssetCheckResult:
    passed = blame_capture.files_captured > 0

    if passed:
        description = (
            f"{blame_capture.files_captured} of {blame_capture.files_tracked} tracked "
            f"files captured ({blame_capture.bytes_written} bytes)."
        )
    else:
        description = (
            f"{blame_capture.slug} produced no blame output from "
            f"{blame_capture.files_tracked} tracked files "
            f"({blame_capture.files_skipped_binary} binary, "
            f"{blame_capture.files_failed} failed). This is the legacy "
            "silent-zero case: a run that finishes having captured nothing."
        )

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=description,
        metadata={
            "files_tracked": blame_capture.files_tracked,
            "files_captured": blame_capture.files_captured,
            "files_skipped_binary": blame_capture.files_skipped_binary,
            "files_failed": blame_capture.files_failed,
            "bytes_written": blame_capture.bytes_written,
        },
    )


# The 14 fields git blame emits that the coverage matrix maps to a column.
# Twelve of them appear on every line git blames, so a run where one is never
# populated means the parser dropped it -- legacy stored 2 of the 14 and threw
# the rest away, which is the failure this check exists to make visible.
_REQUIRED_FIELDS = {
    "header sha": lambda r: r.commit_sha,
    "header line count": lambda r: r.consecutive_line_count,
    "author": lambda r: r.author_name,
    "author-mail": lambda r: r.author_email,
    "author-time": lambda r: r.authored_at_epoch,
    "author-tz": lambda r: r.author_utc_offset,
    "committer": lambda r: r.committer_name,
    "committer-mail": lambda r: r.committer_email,
    "committer-time": lambda r: r.committed_at_epoch,
    "summary": lambda r: r.commit_summary,
    "filename": lambda r: r.origin_file_path,
    "blamed path": lambda r: r.blamed_file_path,
}

# The other two are conditional on the repository's shape, not on the parser.
# `previous` is absent when every surviving line comes from a commit with no
# parent -- true of any young repo. `boundary` is absent when no surviving line
# reaches the root commit. Failing a run for either would mean a correct parse
# of a small repository reports as broken, so they warn instead.
_CONDITIONAL_FIELDS = {
    "previous": lambda r: r.previous_commit_sha,
    "boundary": lambda r: r.reaches_history_boundary,
}


@asset_check(
    asset=blame_records,
    description="Every git blame field the schema stores is populated by at least one record.",
)
def records_carry_every_field(blame_records: list[BlameRecord]) -> AssetCheckResult:
    if not blame_records:
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.ERROR,
            description="no records at all, so no field is populated.",
        )

    def unpopulated(fields):
        return sorted(
            name
            for name, read in fields.items()
            if not any(read(record) for record in blame_records)
        )

    missing = unpopulated(_REQUIRED_FIELDS)
    absent = unpopulated(_CONDITIONAL_FIELDS)

    if missing:
        description = f"never populated: {', '.join(missing)}."
    elif absent:
        description = (
            f"all {len(_REQUIRED_FIELDS)} always-present fields populated; "
            f"{', '.join(absent)} absent, which this repository's history can "
            "explain -- no surviving line has one."
        )
    else:
        description = (
            f"all {len(_REQUIRED_FIELDS) + len(_CONDITIONAL_FIELDS)} blame fields populated."
        )

    return AssetCheckResult(
        passed=not missing,
        severity=AssetCheckSeverity.ERROR if missing else AssetCheckSeverity.WARN,
        description=description,
        metadata={
            "records": len(blame_records),
            "fields_required": len(_REQUIRED_FIELDS),
            "fields_missing": len(missing),
            "conditional_fields_absent": ", ".join(absent) if absent else "none",
        },
    )


@asset_check(
    asset=blame_records,
    additional_ins={"blame_capture": AssetIn("blame_capture")},
    description="Every file the capture wrote a section for yields at least one record.",
)
def records_parse_completely(
    blame_records: list[BlameRecord],
    blame_capture: BlameCapture,
) -> AssetCheckResult:
    """A file that reached the capture but parsed to nothing is a parser bug.

    The capture layer already drops files that blame to nothing, so every
    section it wrote must come back out. Counting sections rather than trusting
    the record count is the point: 36,653 records from 240 of 241 files looks
    like a success until someone asks about the missing file.
    """

    parsed_paths = {record.blamed_file_path for record in blame_records}
    expected = blame_capture.files_captured
    passed = len(parsed_paths) == expected

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"{len(parsed_paths)} of {expected} captured files parsed."
            if passed
            else f"{expected - len(parsed_paths)} captured file(s) yielded no records."
        ),
        metadata={
            "records": len(blame_records),
            "files_captured": expected,
            "files_parsed": len(parsed_paths),
        },
    )


@asset_check(
    asset=blame_attribution_table,
    additional_ins={"blame_records": AssetIn("blame_records")},
    description="Line counts in Postgres add up to the number of lines that were parsed.",
)
def attribution_lines_reconcile(
    blame_attribution_table: LoadResult,
    blame_records: list[BlameRecord],
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Counts the rows back out of the database rather than trusting the writer.

    Legacy reported `blame_status: complete` from a variable the writer set
    itself. Asking Postgres what it actually holds is the difference between
    checking the work and reading back the intention.
    """

    with postgres.connect() as connection:
        stored = connection.execute(
            """
            SELECT COALESCE(SUM(lines), 0)
            FROM horizon.blame_attribution
            WHERE run_id = %s AND organization = %s AND repository = %s
            """,
            (
                blame_attribution_table.run_id,
                blame_attribution_table.organization,
                blame_attribution_table.repository,
            ),
        ).fetchone()[0]

    parsed = len(blame_records)
    passed = stored == parsed

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"{stored} lines stored, {parsed} parsed."
            if passed
            else f"{stored} lines stored but {parsed} parsed -- {abs(parsed - stored)} unaccounted for."
        ),
        metadata={
            "lines_stored": int(stored),
            "lines_parsed": parsed,
            "difference": parsed - int(stored),
        },
    )


@asset_check(
    asset=blame_attribution_table,
    description="Every attribution row points at a commit row that exists.",
)
def every_attribution_has_a_commit(
    blame_attribution_table: LoadResult,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """The foreign key should make this impossible, which is the point.

    It runs anyway: a constraint that was never created, or dropped by a hand-run
    migration, fails silently and the orphans only show up as members whose lines
    have no author.
    """

    with postgres.connect() as connection:
        orphans = connection.execute(
            """
            SELECT COUNT(*)
            FROM horizon.blame_attribution AS attribution
            LEFT JOIN horizon.blame_commits AS commits
              ON  commits.run_id       = attribution.run_id
              AND commits.organization = attribution.organization
              AND commits.repository   = attribution.repository
              AND commits.commit_sha   = attribution.commit_sha
            WHERE attribution.run_id = %s
              AND attribution.organization = %s
              AND attribution.repository = %s
              AND commits.commit_sha IS NULL
            """,
            (
                blame_attribution_table.run_id,
                blame_attribution_table.organization,
                blame_attribution_table.repository,
            ),
        ).fetchone()[0]

    return AssetCheckResult(
        passed=orphans == 0,
        severity=AssetCheckSeverity.ERROR,
        description=(
            "every attribution row resolves to a commit."
            if not orphans
            else f"{orphans} attribution row(s) reference a commit that is not stored."
        ),
        metadata={"orphan_rows": int(orphans)},
    )
