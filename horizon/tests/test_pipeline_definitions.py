from dagster import AssetKey, Definitions

from pipeline.assets.parse import _PARSED_ATTRIBUTES
from pipeline.config import PipelineSettings
from pipeline.definitions import defs
from pipeline.resources import (
    GiteaClient,
    GitWorkspace,
    PostgresResource,
    build_resources,
)


ASSET_KEYS = {
    AssetKey("gitea_repository"),
    AssetKey("blame_capture"),
    AssetKey("blame_records"),
    AssetKey("blame_commits_table"),
    AssetKey("blame_attribution_table"),
    AssetKey("file_ownership"),
    AssetKey("member_repository_ownership"),
    AssetKey("member_ownership"),
}


def test_definitions_expose_the_blame_assets():
    assert isinstance(defs, Definitions)
    assert set(defs.resolve_all_asset_keys()) == ASSET_KEYS


def test_definitions_resolve_into_a_repository():
    """This is what `dagster dev` does on boot, so it catches bad wiring here."""
    assert set(defs.get_repository_def().assets_defs_by_key) == ASSET_KEYS


def test_the_layers_depend_on_each_other_in_order():
    """The graph, not the import order, is what makes each layer follow the last."""
    assets_by_key = defs.get_repository_def().assets_defs_by_key

    def deps_of(name):
        key = AssetKey(name)
        return assets_by_key[key].asset_deps[key]

    assert AssetKey("gitea_repository") in deps_of("blame_capture")
    assert AssetKey("blame_capture") in deps_of("blame_records")
    assert AssetKey("blame_records") in deps_of("blame_commits_table")
    # The foreign key is why attribution waits for commits, not a data need.
    assert AssetKey("blame_commits_table") in deps_of("blame_attribution_table")
    assert AssetKey("blame_records") in deps_of("file_ownership")
    assert AssetKey("file_ownership") in deps_of("member_repository_ownership")
    assert AssetKey("member_repository_ownership") in deps_of("member_ownership")
    assert AssetKey("blame_records") in deps_of("member_ownership")


def test_each_layer_is_its_own_group():
    """One box per layer in the UI, so a failure localises to a layer."""
    assets_by_key = defs.get_repository_def().assets_defs_by_key

    groups = {
        key.to_user_string(): definition.group_names_by_key[key]
        for key, definition in assets_by_key.items()
    }
    assert groups == {
        "gitea_repository": "l1_source",
        "blame_capture": "l2_capture",
        "blame_records": "l3_parse",
        "blame_commits_table": "l4_load",
        "blame_attribution_table": "l4_load",
        "file_ownership": "l5_features",
        "member_repository_ownership": "l5_features",
        "member_ownership": "l5_features",
    }


def test_every_check_is_registered_against_its_asset():
    checks = defs.get_repository_def().asset_checks_defs_by_key

    assert {(key.asset_key, key.name) for key in checks} == {
        (AssetKey("blame_capture"), "capture_is_non_empty"),
        (AssetKey("blame_records"), "records_carry_every_field"),
        (AssetKey("blame_records"), "records_parse_completely"),
        (AssetKey("blame_attribution_table"), "attribution_lines_reconcile"),
        (AssetKey("blame_attribution_table"), "every_attribution_has_a_commit"),
    }


def test_blame_records_publishes_its_parsed_attributes():
    definition = defs.get_repository_def().assets_defs_by_key[AssetKey("blame_records")]
    metadata = definition.metadata_by_key[AssetKey("blame_records")]

    attributes = metadata["parsed_attributes"].value
    schema_names = [column.name for column in metadata["dagster/column_schema"].columns]

    assert [attribute["name"] for attribute in attributes] == schema_names
    assert all(attribute["source"] for attribute in attributes)
    assert all(attribute["description"] for attribute in attributes)

    preview = metadata["attribute_preview"].value
    assert len(preview.records) == len(attributes)
    assert [column.name for column in preview.schema.columns] == [
        "attribute",
        "type",
        "source",
        "description",
    ]


def test_ownership_assets_publish_table_schemas_for_dagster():
    assets_by_key = defs.get_repository_def().assets_defs_by_key
    expected = {
        "file_ownership": [
            "organization",
            "repository",
            "file_path",
            "author_name",
            "author_email",
            "surviving_lines",
            "file_lines",
            "ownership_share",
        ],
        "member_repository_ownership": [
            "organization",
            "repository",
            "author_name",
            "author_email",
            "surviving_lines",
            "repository_lines",
            "ownership_share",
            "files_owned",
            "majority_owned_files",
            "rank",
        ],
        "member_ownership": [
            "organization",
            "author_name",
            "author_email",
            "surviving_lines",
            "surviving_commits",
            "files_owned",
            "repositories",
            "majority_owned_files",
            "average_file_share",
        ],
    }

    for name, columns in expected.items():
        metadata = assets_by_key[AssetKey(name)].metadata_by_key[AssetKey(name)]
        assert [column.name for column in metadata["dagster/column_schema"].columns] == columns
        assert metadata["table"] == name


def test_definitions_expose_the_four_shared_resources():
    assert set(defs.resources) == {"gitea", "workspace", "postgres", "sqlite"}


def test_resources_are_built_from_settings():
    settings = PipelineSettings(
        gitea_url="https://git.example.com",
        gitea_api_token="token-value",
        database_url="postgresql://u@127.0.0.1:5433/db",
        sqlite_path="/tmp/horizon-test.sqlite3",
    )

    resources = build_resources(settings)

    assert isinstance(resources["gitea"], GiteaClient)
    assert isinstance(resources["workspace"], GitWorkspace)
    assert isinstance(resources["postgres"], PostgresResource)
    assert resources["gitea"].base_url == "https://git.example.com"
    assert resources["gitea"].api_token == "token-value"
    assert resources["postgres"].database_url == "postgresql://u@127.0.0.1:5433/db"
    assert resources["sqlite"].database_path == "/tmp/horizon-test.sqlite3"


def test_resources_build_even_when_nothing_is_configured():
    """A missing database URL must not stop the UI from booting."""
    resources = build_resources(PipelineSettings())

    assert resources["postgres"].database_url is None
    assert resources["gitea"].base_url is None
    assert resources["sqlite"].database_path == "data/horizon.sqlite3"
