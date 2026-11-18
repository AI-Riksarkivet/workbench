"""Who is asking search, as whom it reads a source, and who decides each read.

Every route needs a person the workbench verified, and every read is made as that person. A rask lakehouse read as the
person decides it on their own token (`CallerDep`), and its refusal is final; a fan-out across corpora leaves out a
corpus the lakehouse refuses (`_fused_search` skips it). Every other source (a lance-namespace catalog, a bare table, a
lakehouse read as the workbench) knows nobody, so the workbench's own OpenFGA decides: the caller every read is made as
carries the sources it admits them to (`client_kit.media.source_gate.admit`), and a read of any other is refused (403) as
it resolves its source, the default dataset included (`client_kit.sources.require_admitted`); a fan-out is refused
outright when it names a corpus of such a source (`READS_DATASET`), rather than skipping it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from client_kit.media.deps import StateDep
from client_kit.media.source_gate import admit, require_read
from search.core.config import SearchSettings, get_search_settings
from service_kit.governed.deps import FgaChecker, make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller


SettingsDep = Annotated[SearchSettings, Depends(get_search_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The workbench's own OpenFGA, asked about sources no catalog decides for. It allows everything with authorization off,
#: which a service declaring such a source has only with sign-in off (`GovernedMediaSettings`).
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]
#: The corpora a fan-out names (`?corpus=a&corpus=b`), or ``None`` for a single-corpus search.
FanOut = Annotated[list[str] | None, Query(description="Fan out across these corpora, fused by RRF")]


async def _admitted_caller(caller: Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)], check: CheckerDep, state: StateDep) -> LakehouseCaller:
    return await admit(caller, state.settings.sources, check)


#: The person every read is made as, carrying the sources the workbench's store admits them to.
CallerDep = Annotated[LakehouseCaller, Depends(_admitted_caller)]


async def _reads_dataset(caller: CallerDep, check: CheckerDep, state: StateDep, corpus: FanOut = None) -> None:
    # The corpora are exactly the datasets a fan-out resolves; it skips one whose read is refused, so they are asked here.
    await require_read(state.settings.sources, check, caller.subject, corpus or [])


#: Every search route carries it: a verified person, admitted to the sources they may read (the read refuses the rest),
#: and refused a fan-out naming a corpus they may not read.
READS_DATASET = Depends(_reads_dataset)
