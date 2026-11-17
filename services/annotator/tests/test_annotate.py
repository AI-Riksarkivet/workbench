"""A tag row's id: deterministic, so a re-tag is idempotent, and never a drawn shape's.

The tag door's write over HTTP (insert-if-absent into the task's draft, a removal by this id) is pinned in
`test_a_projects_roles_are_grants_in_the_workbench_store.py`.
"""

from __future__ import annotations

from annotator.annotations.tags import tag_id


def test_tag_id_is_deterministic_namespaced_and_collision_safe() -> None:
    a = tag_id("t1", "d1", [0, 19], "speech")
    assert a == tag_id("t1", "d1", [0, 19], "speech")  # deterministic → re-tag is idempotent
    assert a.startswith("tag:")  # the guard: never equals a drawn shape's random id
    assert tag_id("t1", "d1", [0, 19], "a:b") != tag_id("t1", "d1", [0, 19], "a")  # label sep can't collide
    assert tag_id("t1", "d1", [0, 19], "x") != tag_id("t1", "d1", [0, 20], "x")  # per-chunk
    assert tag_id("t1", "d1", [0, 19], "x") != tag_id("t2", "d1", [0, 19], "x")  # per-task: a replica tags its own
