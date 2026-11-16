"""Who is asking the viewer, as whom it reads a source, and who decides each read.

Every route needs a person the workbench verified (`AUTHENTICATED`), and every read is made as that person. Who decides
a read is the source's (`client_kit.sources`): a rask lakehouse read as the person decides it on their own token, which
the viewer exchanges or passes on for them (`CallerDep`), and its refusal reaches the caller as the lakehouse gave it.
Every other source (a lance-namespace catalog, a bare table, a lakehouse read as the workbench) knows nobody, so the
workbench's own OpenFGA decides: `READS_DATASET` refuses (403) a person who is not a declared reader of the source a
request names (`client_kit.media.source_gate`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from client_kit.media.deps import StateDep
from client_kit.media.source_gate import named_in, require_read
from service_kit.governed.deps import FgaChecker, make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller
from viewer.core.config import ViewerSettings, get_viewer_settings


SettingsDep = Annotated[ViewerSettings, Depends(get_viewer_settings)]

_deps = make_auth_deps(SettingsDep)

current_subject = _deps.current_subject
get_checker = _deps.get_checker

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The workbench's own OpenFGA, asked about sources no catalog decides for; it allows everything with authorization off.
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]
#: The person every lakehouse read is made as.
CallerDep = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]
#: As `CallerDep`, anonymous when no token is presented: for the health badge, whose contract is always 200.
OptionalCallerDep = Annotated[LakehouseCaller, Depends(_deps.optional_lakehouse_caller)]
#: The decorator dependency a route that names no dataset carries: a verified person, or a 401.
AUTHENTICATED = Depends(_deps.current_subject)


async def _reads_dataset(request: Request, subject: CurrentSubject, check: CheckerDep, state: StateDep) -> None:
    await require_read(state.settings.sources, check, subject, named_in(request, state.settings.default_dataset))


#: The decorator dependency every route reading a dataset carries: a verified person who may read its source, or a 401
#: or a 403.
READS_DATASET = Depends(_reads_dataset)
