"""The Dagster entrypoint: ``dagster dev -m pipeline.definitions``."""

from __future__ import annotations

from dagster import Definitions

from .assets import (
    blame_attribution_table,
    blame_capture,
    blame_commits_table,
    blame_records,
    file_ownership,
    gitea_repository,
    member_ownership,
    member_repository_ownership,
)
from .checks import (
    attribution_lines_reconcile,
    capture_is_non_empty,
    every_attribution_has_a_commit,
    records_carry_every_field,
    records_parse_completely,
)
from .resources import build_resources

defs = Definitions(
    assets=[
        gitea_repository,
        blame_capture,
        blame_records,
        blame_commits_table,
        blame_attribution_table,
        file_ownership,
        member_repository_ownership,
        member_ownership,
    ],
    asset_checks=[
        capture_is_non_empty,
        records_carry_every_field,
        records_parse_completely,
        attribution_lines_reconcile,
        every_attribution_has_a_commit,
    ],
    resources=build_resources(),
)
