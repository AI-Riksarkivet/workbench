"""Who is asking search, as whom it reads a source, and who decides each read.

Every route needs a person the workbench verified, and every read is made as that person. A rask lakehouse read as the
person decides it on their own token (`CallerDep`), and its refusal is final; a fan-out across corpora leaves out a
corpus the lakehouse refuses (`_fused_search` skips it). Every other source (a lance-namespace catalog, a bare table, a
lakehouse read as the workbench) knows nobody, so the workbench's own OpenFGA decides: `READS_DATASET` refuses (403) a
person who is not a declared reader of a source the request names (`client_kit.media.source_gate`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from client_kit.media.deps import StateDep
from client_kit.media.source_gate import named_in, require_read
from search.core.config import SearchSettings, get_search_settings
from service_kit.governed.deps import FgaChecker, make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller


SettingsDep = Annotated[SearchSettings, Depends(get_search_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The workbench's own OpenFGA, asked about sources no catalog decides for; it allows everything with authorization off.
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]
#: The person every lakehouse read is made as.
CallerDep = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]
#: The corpora a fan-out names (`?corpus=a&corpus=b`), or ``None`` for a single-corpus search.
FanOut = Annotated[list[str] | None, Query(description="Fan out across these corpora, fused by RRF")]


async def _reads_dataset(request: Request, subject: CurrentSubject, check: CheckerDep, state: StateDep) -> None:
    await require_read(state.settings.sources, check, subject, named_in(request, state.settings.default_dataset))


#: Every search route carries it: a verified person who may read the source of each dataset the request names.
READS_DATASET = Depends(_reads_dataset)
