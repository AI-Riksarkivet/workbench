"""Whether a person may read a source the workbench decides for (`client_kit.sources.workbench_decides`).

A lakehouse read as the person decides every read itself, on the person's token, and is not asked here. Every other
source (a lance-namespace catalog, a bare table, a lakehouse read as the workbench) has no catalog that knows the
person, so the workbench's own OpenFGA decides: ``can_read`` on ``source:<name>``, which the model writer converges from
the source's declared readers. Every door that touches such a source's data asks here first; with authorization off
(the local stack, sign-in off) the checker allows everything, as it does for every other check.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.requests import Request

from client_kit.sources import Sources, workbench_decides
from service_kit.exceptions import ForbiddenError
from service_kit.governed.deps import FgaChecker


def source_object(name: str) -> str:
    return f"source:{name}"


async def may_read(sources: Sources, check: FgaChecker, subject: str, name: str) -> bool:
    """Whether ``subject`` may read source ``name`` (always, for a source the lakehouse decides)."""
    source = sources.named(name)
    return not workbench_decides(source) or await check(user=subject, relation="can_read", obj=source_object(name))


async def require_read(sources: Sources, check: FgaChecker, subject: str, names: Iterable[str]) -> None:
    """Refuse (403) when any of ``names`` (dataset or table ids, ``<source>~<id>``) is of a source ``subject`` may not
    read. A name naming no declared source is left to the door, which answers it as unknown.

    Raises:
        ForbiddenError: ``subject`` may not read one of the sources named.
    """
    seen: set[str] = set()
    for qualified in names:
        try:
            source = sources.locate(qualified).source
        except ValueError:
            continue
        if source.name in seen:
            continue
        seen.add(source.name)
        if not await may_read(sources, check, subject, source.name):
            raise ForbiddenError(f"{subject} may not read source {source.name!r}")


def named_in(request: Request, default: str | None) -> list[str]:
    """The dataset and table ids a request names (``{dataset_id}``, ``?dataset=``, ``?table=``, each ``?corpus=``), or
    the default dataset a request naming none reads."""
    named = [v for v in (request.path_params.get("dataset_id"), request.query_params.get("dataset"), request.query_params.get("table")) if v]
    named += request.query_params.getlist("corpus")
    return named or ([default] if default else [])


async def readable(sources: Sources, check: FgaChecker, subject: str) -> set[str]:
    """The names of the sources ``subject`` may read."""
    return {source.name for source in sources.items if await may_read(sources, check, subject, source.name)}


__all__ = ["may_read", "named_in", "readable", "require_read", "source_object"]
