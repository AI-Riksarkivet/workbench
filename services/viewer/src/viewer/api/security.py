"""Who is asking the viewer, as whom it reads a source, and who decides each read.

Every route needs a person the workbench verified (`AUTHENTICATED`), and every read is made as that person. Who decides
a read is the source's (`client_kit.sources`): a rask lakehouse read as the person decides it on their own token, which
the viewer exchanges or passes on for them (`CallerDep`), and its refusal reaches the caller as the lakehouse gave it.
Every other source (a lance-namespace catalog, a bare table, a lakehouse read as the workbench) knows nobody, so the
workbench's own OpenFGA decides: the caller every read is made as carries the sources it admits them to
(`client_kit.media.source_gate.admit`), and a read of any other is refused (403) as it resolves its source, the default
dataset included (`client_kit.sources.require_admitted`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from client_kit.media.deps import StateDep
from client_kit.media.source_gate import admit
from service_kit.governed.deps import FgaChecker, make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller
from viewer.core.config import ViewerSettings, get_viewer_settings


SettingsDep = Annotated[ViewerSettings, Depends(get_viewer_settings)]

_deps = make_auth_deps(SettingsDep)

current_subject = _deps.current_subject
get_checker = _deps.get_checker

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The workbench's own OpenFGA, asked about sources no catalog decides for. It allows everything with authorization off,
#: which a service declaring such a source has only with sign-in off (`GovernedMediaSettings`).
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]


async def _admitted_caller(caller: Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)], check: CheckerDep, state: StateDep) -> LakehouseCaller:
    return await admit(caller, state.settings.sources, check)


#: The person every read is made as, carrying the sources the workbench's store admits them to.
CallerDep = Annotated[LakehouseCaller, Depends(_admitted_caller)]
#: As `CallerDep`, anonymous when no token is presented: for the health badge, whose contract is always 200.
OptionalCallerDep = Annotated[LakehouseCaller, Depends(_deps.optional_lakehouse_caller)]
#: The decorator dependency a route that names no dataset carries: a verified person, or a 401.
AUTHENTICATED = Depends(_deps.current_subject)


#: The decorator dependency every route reading a dataset carries: a verified person, admitted to the sources the
#: workbench decides for that they may read (a 401 otherwise); the read refuses the rest (403).
READS_DATASET = Depends(_admitted_caller)
