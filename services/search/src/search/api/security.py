"""Who is asking search, and as whom it reads the lakehouse.

Search holds no gate of its own on a corpus: a corpus is the lakehouse's data, and the lakehouse decides who may
search it. Every route needs a person the workbench verified, and every read is made as that person, on a token
search exchanges for one addressed to the lakehouse (`CallerDep`); a refusal there is final. A fan-out across corpora
leaves out a corpus the lakehouse refuses (`_fused_search` skips it), so the answer to "what may I search" is the
shorter list, not a 403.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from search.core.config import SearchSettings, get_search_settings
from service_kit.governed.deps import make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller


SettingsDep = Annotated[SearchSettings, Depends(get_search_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The person every lakehouse read is made as.
CallerDep = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]
#: The corpora a fan-out names (`?corpus=a&corpus=b`), or ``None`` for a single-corpus search.
FanOut = Annotated[list[str] | None, Query(description="Fan out across these corpora, fused by RRF")]
