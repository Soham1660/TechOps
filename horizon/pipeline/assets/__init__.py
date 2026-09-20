"""Asset definitions, one module per layer of the graph.

Layers, not families of convenience: the source layer talks to Gitea, the
capture layer is the only one touching the network and the filesystem, and the
parse layer is pure. Keeping them in separate modules is what lets the pure one
be tested exhaustively without a repo anywhere in sight.

One Dagster group per layer, numbered ``l1_``..``l6_``. The number is there
because the UI sorts groups alphabetically, so the numbering makes the graph
read top to bottom in the order data moves through it; a failure then localises
to a layer -- and each layer has exactly one job -- instead of to one large box
of assets. Later PRs add ``l6_mart``.
"""

from .capture import BlameCapture, blame_capture
from .features import file_ownership, member_ownership, member_repository_ownership
from .load import LoadResult, blame_attribution_table, blame_commits_table
from .parse import blame_records
from .source import RepositoryConfig, gitea_repository

__all__ = [
    "BlameCapture",
    "LoadResult",
    "RepositoryConfig",
    "blame_attribution_table",
    "blame_commits_table",
    "blame_capture",
    "blame_records",
    "file_ownership",
    "gitea_repository",
    "member_ownership",
    "member_repository_ownership",
]
