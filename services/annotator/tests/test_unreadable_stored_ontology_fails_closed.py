"""A stored ontology the current model cannot parse is refused BY NAME, not as an anonymous 500.

The ontology is a CLOSED-SET contract: `send`, `import`, `assist` and `submit` all judge shapes
against it, and the publish path stamps its class list into the run facet. So a stored ontology the
running code cannot read cannot be treated as "constrains nothing" — that silently converts a
project with rules into a project with none, on the exact paths (bulk send, import, publish) where
one click is hundreds of rows. Fail closed.

And name it: the state store validates every document it reads, so an unreadable ontology would otherwise surface as
a bare `pydantic.ValidationError`, a 500 naming neither the project nor the reason. These pin the named refusal: a
`DomainError` naming the project (or the task) and the field that could not be read. The documents are planted in the
test's own PostgreSQL database the way a replica on an older build would have written them, which is how a rolling
upgrade that changes `LabelOntology` reaches the readers on the new build.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from annotator.core.config import AnnotatorSettings
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase
from workbench.exceptions import DomainError


#: An ontology document an OLDER model version would have written. Before the ontology merge the
#: project carried a separate `template` object holding `required_labels: list[str]`; that field has
#: no home on `LabelOntology`, whose `extra="forbid"` therefore refuses the whole document. This is
#: the realistic shape of the failure — not a corrupt blob, but yesterday's schema.
LEGACY_ONTOLOGY: dict[str, Any] = {
    "kind": "object-detection",
    "classes": [{"name": "seal", "tools": ["bbox"]}],
    "required_labels": ["seal"],
}


@pytest_asyncio.fixture
async def store(state_settings: AnnotatorSettings, state_database: StateDatabase) -> AsyncIterator[AnnotatorStore]:
    """A project `p-charters` and its task `t-77`, both rewritten as an older build stored them."""
    opened = await AnnotatorStore.open(state_settings)
    await opened.create_project(AnnotationProject(project_id="p-charters", tenant="acme", slug="charters", state=ProjectState.LABELING))
    await opened.send_tasks(
        "p-charters", [Task(task_id="t-77", project_id="p-charters", source=ItemSource(kind="chunks", keys=["t-77"]), media=MediaRef(kind="image"))]
    )
    with psycopg.connect(host=state_database.host, port=state_database.port, user=state_database.user, dbname=state_database.database) as conn:
        for table, key in (("projects", "project_id"), ("tasks", "task_id")):
            conn.execute(
                f"UPDATE {table} SET document = jsonb_set(document, '{{ontology}}', %s)".encode() + f" WHERE {key} IS NOT NULL".encode(),
                (Jsonb(LEGACY_ONTOLOGY),),
            )
    yield opened
    await opened.close()


@pytest.mark.asyncio
async def test_the_project_seam_names_the_project_and_the_reason(store: AnnotatorStore) -> None:
    """The identifier is the whole point of the refusal: an operator reading it goes to the one project whose document
    needs rewriting, without a log dive."""
    with pytest.raises(DomainError) as caught:
        await store.get_project("p-charters")

    detail = str(caught.value)
    assert "ontology" in detail and "p-charters" in detail, detail
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"], "the refusal does not say which field to rewrite"


@pytest.mark.asyncio
async def test_the_task_seam_names_the_task_and_the_reason(store: AnnotatorStore) -> None:
    """The task carries its OWN captured copy of the ontology, so it is a second, independent seam."""
    with pytest.raises(DomainError) as caught:
        await store.get_task("t-77")

    detail = str(caught.value)
    assert "ontology" in detail and "t-77" in detail, detail
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"]
