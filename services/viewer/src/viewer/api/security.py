"""Who is asking the viewer, and as whom it reads the lakehouse.

The viewer holds no gate of its own on a corpus: a corpus is the lakehouse's data, and the lakehouse decides who may
read it. Every route needs a person the workbench verified (`AUTHENTICATED`), and every read is made as that person,
on a token the viewer exchanges for one addressed to the lakehouse (`CallerDep`); a refusal there is final and reaches
the caller as the lakehouse gave it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from service_kit.governed.deps import make_auth_deps
from service_kit.governed.token_exchange import LakehouseCaller
from viewer.core.config import ViewerSettings, get_viewer_settings


SettingsDep = Annotated[ViewerSettings, Depends(get_viewer_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
#: The person every lakehouse read is made as.
CallerDep = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]
#: As `CallerDep`, anonymous when no token is presented: for the health badge, whose contract is always 200.
OptionalCallerDep = Annotated[LakehouseCaller, Depends(_deps.optional_lakehouse_caller)]
#: The decorator dependency every corpus route carries: a verified person, or a 401.
AUTHENTICATED = Depends(_deps.current_subject)
