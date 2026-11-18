"""Whether a person may read a source the workbench decides for (`client_kit.sources.workbench_decides`).

A lakehouse read as the person decides every read itself, on the person's token, and is not asked here. Every other
source (a lance-namespace catalog, a bare table, a lakehouse read as the workbench) has no catalog that knows the
person, so the workbench's own OpenFGA decides: ``can_read`` on ``source:<name>``, which the model writer converges from
the source's declared readers. A request's caller carries the answer (:func:`admit`), and every read of a source checks
it as it resolves the source (`client_kit.sources.require_admitted`); a door naming its sources up front (a signing, a
send, a fan-out) asks :func:`require_read` too, to refuse before any work. The checker allows
everything only with authorization off, which a service declaring such a source runs only with sign-in off too (the local
stack, where everyone is `anon`): with sign-in on it refuses to start without the store (`GovernedMediaSettings`).
"""

from __future__ import annotations

from collections.abc import Iterable

from client_kit.sources import Sources, workbench_decides
from service_kit.exceptions import ForbiddenError
from service_kit.governed.deps import FgaChecker
from service_kit.governed.token_exchange import LakehouseCaller


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


async def readable(sources: Sources, check: FgaChecker, subject: str) -> set[str]:
    """The names of the sources ``subject`` may read."""
    return {source.name for source in sources.items if await may_read(sources, check, subject, source.name)}


async def admit(caller: LakehouseCaller, sources: Sources, check: FgaChecker) -> LakehouseCaller:
    """``caller``, carrying the sources the workbench's store admits them to read (`LakehouseCaller.admitted`), which
    every read of a source checks as it resolves it (`client_kit.sources.require_admitted`). So the gate is the read
    itself: a dataset a door reads, the default included, is checked whatever else the request names."""
    return caller.model_copy(update={"admitted": frozenset(await readable(sources, check, caller.subject))})


__all__ = ["admit", "may_read", "readable", "require_read", "source_object"]
