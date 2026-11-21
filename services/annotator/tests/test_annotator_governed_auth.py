"""The annotator's governed-auth seam: a VERIFIED subject, and authz that fails closed.

`services/annotator` served the read-plane and took its author from a trusted `X-User` header
(`workbench.media.deps.get_author`, defaulting to `"anon"`). That is fine for reads and a
cross-user leak for annotation projects, whose every entity is keyed on who owns or claims it.

These tests pin the two properties that make the difference real (the door itself, token and proof, is
`packages/workbench/tests/test_every_verifier_failure_is_a_401_or_a_503.py`'s):

1. the subject comes from a **verified token**, with no header fallback; and
2. an auth layer that is *enabled but broken* returns **503**, never open access.

The second is the one worth having a test for: fail-open is silent, ships green, and is only
discovered by someone reading another user's tasks.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import (
    ANONYMOUS_SUBJECT,
    CheckerDep,
    CurrentSubject,
)
from annotator.core.config import AnnotatorSettings
from workbench.exceptions import register_handlers
from workbench.ns_errors import install_problem_handlers


def _app(settings: AnnotatorSettings, **state: Any) -> FastAPI:
    """A one-route app wired exactly like the real service, with `app.state` under test control.

    BOTH handler installers, because `annotator/main.py` calls both (lines 186 + 196) and the second
    one is the reason the auth refusals render at all: the governed kernel raises the `lance_namespace`
    taxonomy, which `register_handlers` does not map. Installing only the fleet half made this fixture
    claim a wiring the service does not have, and a refusal that reaches a real user through
    `install_problem_handlers` was being asserted here against starlette's fallback instead.
    """
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    for k, v in state.items():
        setattr(app.state, k, v)

    @app.get("/whoami")
    def whoami(subject: CurrentSubject) -> dict[str, str]:
        return {"subject": subject}

    @app.get("/guarded")
    async def guarded(subject: CurrentSubject, checker: CheckerDep) -> dict[str, bool]:
        return {"allowed": await checker(user=subject, relation="can_view", obj="project:acme")}

    app.dependency_overrides[type(settings)] = lambda: settings
    from annotator.core.config import get_annotator_settings

    app.dependency_overrides[get_annotator_settings] = lambda: settings
    return app


def _settings(**kw: Any) -> AnnotatorSettings:
    return AnnotatorSettings.model_construct(**kw)


# --------------------------------------------------------------------------------------------------
# The subject
# --------------------------------------------------------------------------------------------------


def test_an_x_user_header_can_no_longer_choose_the_subject() -> None:
    """THE regression guard. `X-User` used to BE the identity; it must now be inert.

    If this ever fails, any caller can act as any user by setting a header.
    """
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=False)))
    got = client.get("/whoami", headers={"X-User": "attacker"}).json()
    assert got == {"subject": ANONYMOUS_SUBJECT}
    assert got["subject"] != "attacker"


# --------------------------------------------------------------------------------------------------
# Fail closed — the property that must never regress
# --------------------------------------------------------------------------------------------------


def test_authorization_on_but_no_store_to_ask_is_503_not_permissive() -> None:
    """The dangerous one: a broken authz layer must not silently become an open one. With authorization on, every
    decision is the workbench store's answer, so a process that cannot ask it (no store on the app, here) refuses."""
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=True), authz=None))
    assert client.get("/guarded").status_code == 503


# --------------------------------------------------------------------------------------------------
# The settings invariant
# --------------------------------------------------------------------------------------------------


def test_authorization_without_authentication_is_refused_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """FGA answers "may THIS subject", so enabling it without OIDC would check an unverified subject.

    Caught when settings are built, not at the first request.

    Driven through the ENVIRONMENT rather than kwargs: `LANCE_*` are the field aliases, so this also
    proves the names an operator actually sets are the ones that bind. Passing them as keyword
    arguments would exercise a path no deployment uses.
    """
    monkeypatch.setenv("WORKBENCH_FGA_ENABLED", "true")
    monkeypatch.setenv("WORKBENCH_OIDC_ENABLED", "false")
    with pytest.raises(ValueError, match="WORKBENCH_OIDC_ENABLED is required"):
        AnnotatorSettings()


# ── the WRITE routes' author stamp ──────────────────────────────────────────────────────────────
#
# — "Write authorship comes from an unverified, client-supplied `X-User` header".
#
# This file already pinned the projects plane ("X-User used to BE the identity; it must now be
# inert") — but the two Lance WRITE routes kept the old seam: `save_annotations` and `apply_tags`
# stamped `workbench.media.deps.get_author`'s value as the reviewer, so any caller could sign
# another person's name onto annotation provenance by setting a header. The independent re-audit
# (2026-08-28) confirmed it live at HEAD and noted the sharp part: the gateway's spoofable-header
# strip covered `x-forwarded-*` but not `x-user`, so the header sailed through the edge.


def _author_dependency(route_fn: object) -> object:
    """The callable FastAPI will actually run to fill the `author` parameter."""
    import typing

    hints = typing.get_type_hints(route_fn, include_extras=True)
    metadata = typing.get_args(hints["author"])[1:]
    for item in metadata:
        dependency = getattr(item, "dependency", None)
        if dependency is not None:
            return dependency
    raise AssertionError("`author` carries no dependency at all")


@pytest.mark.parametrize(
    "route",
    ["save_annotations"],
)
def test_the_write_routes_take_their_author_from_the_VERIFIED_subject(route: str) -> None:
    """Asserted on the dependency IDENTITY: FastAPI runs exactly this callable, so the author can
    only be what `current_subject` answers — the verified principal, `anon` with OIDC off, 401 with OIDC
    on and no token, 503 when enabled-but-unwired. No header is consulted anywhere on that path."""
    from annotator.annotations import save, tags
    from annotator.api import security

    route_fn = getattr(save, route, None) or getattr(tags, route)
    assert _author_dependency(route_fn) is security.current_subject, (
        f"`{route}` still resolves its author through the client-supplied X-User seam — "
        "a caller can sign any name onto annotation provenance by setting a header"
    )
