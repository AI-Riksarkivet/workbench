"""Every setting is read from the one environment name it documents, its alias, and from no other.

A settings class that validates by field name teaches the environment source the field's own name too (with any
`env_prefix` before it), so `DOCS_ENABLED` or `WORKBENCH_STATE_HOST` would set what `WORKBENCH_DOCS` or
`WORKBENCH_FLOWS_STATE_HOST` documents. For each class, each field: its field name, upper-cased, and with each prefix the
repository's names use, set to a value no field accepts, changes nothing; its documented name, set the same way, does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsError

from annotator.core.config import AnnotatorSettings
from flows.config import FlowsSettings
from search.core.config import SearchSettings
from viewer.core.config import ViewerSettings
from workbench.config import Settings
from workbench.governed.auth.write_model import WriterSettings
from workbench.media.config import MediaSettings


PREFIXES = ("", "WORKBENCH_", "LANCE_", "MEDIA_", "VIEWER_", "SEARCH_", "ANNOTATOR_", "WORKBENCH_FLOWS_")
#: Not a value any field takes: read from anywhere, it fails the settings or becomes the field's value.
GARBAGE = "{not-a-value"


def _read(cls: type[BaseSettings]) -> dict[str, object] | str:
    """The settings the environment gives, or why they could not be read (the repository holds no `.env`)."""
    try:
        return cls.model_validate({}).model_dump()
    except (ValidationError, SettingsError, ValueError) as exc:
        return f"refused: {type(exc).__name__}"


CLASSES = [Settings, MediaSettings, ViewerSettings, SearchSettings, AnnotatorSettings, FlowsSettings, WriterSettings]
#: What a pod's environment holds besides the settings: the platform's, other services', names no setting has.
NOISE = {
    "HOSTNAME": "pod-1",
    "KUBERNETES_SERVICE_HOST": "10.43.0.1",
    "OTEL_SERVICE_NAME": "viewer",
    "WORKBENCH_NOT_A_SETTING": "1",
    "VIEWER_NOT_A_SETTING": "1",
    "SEARCH_NOT_A_SETTING": "1",
    "ANNOTATOR_NOT_A_SETTING": "1",
    "MEDIA_DOCS": "true",
    "LANCE_AUDIT_ENABLED": "false",
}


def _required(cls: type[BaseSettings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if cls is WriterSettings:
        # The model writer's required names; the suite's media sources are given inline, which it does not read.
        monkeypatch.setenv("WORKBENCH_FGA_API_URL", "http://fga.example")
        monkeypatch.setenv("WORKBENCH_FGA_ORGS_FILE", str(tmp_path / "orgs.json"))
        monkeypatch.setenv("WORKBENCH_SOURCES_FILE", str(tmp_path / "sources.json"))


@pytest.mark.parametrize("cls", CLASSES)
def test_a_setting_given_by_its_field_name_is_refused(cls: type[BaseSettings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _required(cls, tmp_path, monkeypatch)
    taken: list[str] = []
    for name in cls.model_fields:
        try:
            cls.model_validate({name: GARBAGE})
        except ValidationError as exc:
            if any(error["type"] == "extra_forbidden" and error["loc"] == (name,) for error in exc.errors()):
                continue
        taken.append(name)

    assert taken == []


@pytest.mark.parametrize("cls", CLASSES)
def test_variables_no_setting_names_are_left_alone(cls: type[BaseSettings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _required(cls, tmp_path, monkeypatch)
    baseline = _read(cls)
    for name, value in NOISE.items():
        monkeypatch.setenv(name, value)

    assert (isinstance(baseline, dict), _read(cls)) == (True, baseline)


@pytest.mark.parametrize("cls", CLASSES)
def test_a_setting_is_read_from_its_documented_name_alone(cls: type[BaseSettings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _required(cls, tmp_path, monkeypatch)
    baseline = _read(cls)
    aliases = {field.alias.upper() for field in cls.model_fields.values() if field.alias}
    twins_read: list[str] = []
    documented_ignored: list[str] = []
    for name, field in cls.model_fields.items():
        twins = {f"{prefix}{name}".upper() for prefix in PREFIXES} - aliases
        with monkeypatch.context() as env:
            for twin in twins | {name}:
                env.setenv(twin, GARBAGE)
            if _read(cls) != baseline:
                twins_read.append(name)
        with monkeypatch.context() as env:
            env.setenv(str(field.alias), GARBAGE)
            if _read(cls) == baseline:
                documented_ignored.append(name)

    assert (isinstance(baseline, dict), twins_read, documented_ignored) == (True, [], [])


@pytest.mark.parametrize(
    "given",
    [
        pytest.param("https://a.example", id="one"),
        pytest.param("https://a.example, https://b.example", id="comma-separated"),
        pytest.param('["https://a.example", "https://b.example"]', id="json"),
    ],
)
def test_a_list_of_strings_is_read_from_the_environment_as_a_json_list_or_comma_separated(given: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_CORS_ORIGINS", given)
    monkeypatch.setenv("WORKBENCH_OIDC_SUBJECT_CLAIMS", given)

    settings = ViewerSettings()

    expected = ["https://a.example"] if "b.example" not in given else ["https://a.example", "https://b.example"]
    assert (settings.cors_origins, settings.oidc_subject_claims) == (expected, expected)
