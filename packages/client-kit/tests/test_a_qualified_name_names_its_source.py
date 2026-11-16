"""A dataset's or table's name carries its source (``<source>:<id>``), and each source admits the ids its kind can hold.

A lakehouse id names its warehouse first (`docs/rask/COMMIT-EVENTS.md`: a one-segment namespace is a warehouse, which
holds no tables); any other source's datasets sit at any depth, the root included; a source naming one ``.lance`` table
holds that table alone; and no segment can be read as a path (empty, ``.``, ``..``, a slash or backslash, a control
character, a delimiter). The declaration is refused at load when a deployment could not be read as written: no source, a
declaration given twice, two sources of one name, two lakehouses, a key the allow-list does not hold (every documented
credential spelling among them; `client_kit.source_keys`), a credential in a location, a REST lakehouse read as nobody,
or a default dataset no source holds; and a harmless key the old deny-list refused is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from client_kit.media.config import Settings


SOURCES = [
    {"kind": "rask", "name": "lake", "dir": "/catalog", "identity": "none"},
    {"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": "/archive"}},
    {"kind": "tables", "name": "one", "uri": "/data/scans.lance"},
]


def _settings(**env: object) -> Settings:
    return Settings.model_validate({"WORKBENCH_SOURCES": SOURCES, **env})


@pytest.mark.parametrize(
    ("qualified", "table", "source", "expected"),
    [
        pytest.param("lake~acme$bronze$scans", "pages", "lake", ["acme", "bronze", "scans", "pages"], id="lakehouse-nested"),
        pytest.param("archive~", "pages", "archive", ["pages"], id="root-of-a-namespace"),
        pytest.param("one~", "scans", "one", ["scans"], id="the-one-table"),
    ],
)
def test_a_table_is_its_sources_namespace_then_its_name(qualified: str, table: str, source: str, expected: list[str]) -> None:
    located = _settings().sources.table(qualified, table)
    assert (located.source.name, located.identifier) == (source, expected)


@pytest.mark.parametrize(
    ("qualified", "table", "error"),
    [
        pytest.param("acme$bronze", "pages", "names no source", id="no-source"),
        pytest.param("elsewhere~acme$bronze", "pages", "no source is named 'elsewhere'", id="undeclared-source"),
        pytest.param("lake~bronze", "pages", "not warehouse-qualified", id="lakehouse-one-segment"),
        pytest.param("lake~acme$", "pages", "names no table or namespace", id="empty-segment"),
        pytest.param("lake~acme$bronze", "a$b", "holds a delimiter", id="delimiter-in-table"),
        pytest.param("lake~acme$bronze", "", "names no table or namespace", id="empty-table"),
        pytest.param("archive~..", "pages", "names no table or namespace", id="dot-dot"),
        pytest.param("archive~a", "../outside", "path or control character", id="slash"),
        pytest.param("archive~a", "a\\b", "path or control character", id="backslash"),
        pytest.param("archive~a", "a\x00b", "path or control character", id="nul"),
        pytest.param("archive~a", "a\nb", "path or control character", id="control"),
        pytest.param("archive~a", "x~y", "holds a delimiter", id="source-separator"),
    ],
)
def test_a_name_its_source_could_not_hold_is_refused(qualified: str, table: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _settings().sources.table(qualified, table)


def test_a_source_naming_one_table_holds_no_other() -> None:
    with pytest.raises(ValueError, match="holds the one table 'scans'"):
        _settings().sources.table_named("one~other")


@pytest.mark.parametrize(
    ("declared", "error"),
    [
        pytest.param({}, "set exactly one of WORKBENCH_SOURCES_FILE", id="none"),
        pytest.param({"WORKBENCH_SOURCES_FILE": "FILE", "WORKBENCH_SOURCES": SOURCES}, "set exactly one of WORKBENCH_SOURCES_FILE", id="both"),
        pytest.param({"WORKBENCH_SOURCES": [SOURCES[0], {**SOURCES[1], "name": "lake"}]}, "source names must be distinct", id="one-name-twice"),
        pytest.param({"WORKBENCH_SOURCES": [SOURCES[0], {**SOURCES[0], "name": "lake2"}]}, "at most one source is a rask lakehouse", id="two-lakehouses"),
        *(
            pytest.param({"WORKBENCH_SOURCES": [{**SOURCES[2], "storage": {key: "x"}}]}, f"storage option {key!r} is not one", id=key)
            for key in (
                "aws_secret_access_key",
                "account_key",
                "azure_storage_account_key",
                "google_service_account_key",
                "service_account_key",
                "sas_token",
                "base_1.secret_access_key",
            )
        ),
        *(
            pytest.param({"WORKBENCH_SOURCES": [{**SOURCES[1], "properties": {"root": "/archive", key: "x"}}]}, f"property {key!r} is not one", id=key)
            for key in ("header.Authorization", "header.Cookie", "headers.Authorization", "storage.account_key")
        ),
        pytest.param(
            {"WORKBENCH_SOURCES": [{**SOURCES[2], "storage": {"google_service_account": "/etc/sa.json"}}]},
            "credential file outside",
            id="credential-file-elsewhere",
        ),
        pytest.param({"WORKBENCH_SOURCES": [{**SOURCES[2], "uri": "s3://me:pw@bucket/t.lance"}]}, "carries a user or password", id="userinfo"),
        pytest.param({"WORKBENCH_SOURCES": [{**SOURCES[2], "uri": "https://store/t.lance?X-Amz-Signature=abc"}]}, "query or fragment", id="presigned"),
        pytest.param(
            {"WORKBENCH_SOURCES": [{"kind": "rask", "name": "lake", "uri": "http://catalog", "identity": "none"}]},
            "never as nobody",
            id="rest-lakehouse-as-nobody",
        ),
        pytest.param(
            {"WORKBENCH_SOURCES": [{"kind": "namespace", "name": "x", "impl": "rest", "properties": {"uri": "http://c"}}]},
            "names no `locations`",
            id="rest-no-locations",
        ),
        pytest.param({"WORKBENCH_SOURCES": SOURCES, "MEDIA_DEFAULT_DATASET": "lake~bronze"}, "not warehouse-qualified", id="default-no-source-holds"),
    ],
)
def test_a_declaration_that_cannot_be_read_as_written_fails_the_settings(
    declared: dict[str, object], error: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WORKBENCH_SOURCES")
    declaration = tmp_path / "sources.json"
    declaration.write_text(json.dumps({"sources": SOURCES}))
    given = {name: (str(declaration) if value == "FILE" else value) for name, value in declared.items()}
    with pytest.raises(ValidationError, match=error):
        Settings.model_validate(given)


def test_the_declaration_file_is_what_the_settings_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKBENCH_SOURCES")
    declaration = tmp_path / "sources.json"
    declaration.write_text(json.dumps({"sources": SOURCES}))
    assert [s.name for s in Settings.model_validate({"WORKBENCH_SOURCES_FILE": str(declaration)}).sources.items] == ["lake", "archive", "one"]


def test_a_harmless_storage_key_the_deny_list_refused_is_accepted() -> None:
    harmless = {"aws_role_session_name": "workbench", "aws_role_arn": "arn:aws:iam::1:role/r", "region": "eu-north-1"}

    settings = Settings.model_validate({"WORKBENCH_SOURCES": [{**SOURCES[2], "storage": harmless}]})

    assert settings.sources.named("one").model_dump()["storage"] == harmless
