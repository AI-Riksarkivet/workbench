"""People by name: what the app shows for a principal, from the person's own verified token and from nowhere else.

A principal (`dex~08a8…`) is what every grant, claim and review records, and it is unreadable to a person. A name is
known honestly in one place only: the claims of the token the shared door verified (`service_kit.governed.deps`). So:

- **The signed-in person** reads their own name off their own token at `/me` (`display_name`), and that read REGISTERS
  it (`register_person`): the app calls `/me` once each time a person signs in, so a person granted a role is known by
  name from their first visit, and a renamed person from their next sign-in. It is the one read that writes; every
  other read records nothing, so a page's reads never wait on, or fail with, a write.
- **Everyone else** is named by what the annotator kept the last time THEY signed in or wrote through one of its
  doors: every door that records a person (a create, a send, a transition, a save, an import, a grant, a pick) carries
  `RECORDS_PERSON`, which keeps the name the caller's verified token carried beside their principal (`people` in
  `state/schema.sql`), replacing an older one. No request body ever names a person's name, so no caller can name
  another. A person who has never signed in here (granted a role, not yet arrived) is shown by their principal.
- **A read that shows principals answers their kept names beside them** (`people`), under the door's own check, so a
  name is disclosed only where the principal it belongs to already is.

A name is the token's `name`, else its `preferred_username`, and never its `email`: the first that is still text once
normalised (NFC, control and format characters removed, whitespace collapsed) and cut to `NAME_MAX` characters.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Final

from fastapi import Depends, Request
from psycopg import Error as DatabaseError

from annotator.api.security import CurrentToken
from annotator.state.store import AnnotatorStore


logger = logging.getLogger(__name__)

#: The longest name kept and shown, in characters; the store's column holds the same bound.
NAME_MAX: Final = 120
#: The claims a name is read from, in order of preference. Never the email: a person whose token names no name would
#: have their address shown to everyone who sees a project they work in, where their opaque principal is shown instead.
NAME_CLAIMS: Final = ("name", "preferred_username")
#: The methods that never write, whose callers are therefore not recorded.
_READS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def display_name(claims: Mapping[str, object]) -> str | None:
    """The name a verified token's claims give its person, bounded and cleaned, or None when they give none."""
    for claim in NAME_CLAIMS:
        value = claims.get(claim)
        if not isinstance(value, str):
            continue
        text = "".join(ch for ch in unicodedata.normalize("NFC", value) if not unicodedata.category(ch).startswith("C"))
        text = " ".join(text.split())[:NAME_MAX].strip()
        if text:
            return text
    return None


async def record_person(request: Request, caller: CurrentToken) -> None:
    """Keep the name the writing caller's verified token carries. Reads record nothing (`/me` registers explicitly)."""
    if request.method in _READS:
        return
    await register_person(request, caller)


async def register_person(request: Request, caller: CurrentToken) -> None:
    """Keep the name ``caller``'s verified token carries beside their principal. An anonymous caller and a service with
    no state store record nothing; a store that cannot keep it costs the name, never the request."""
    if caller is None:
        return
    store: AnnotatorStore | None = getattr(request.app.state, "store", None)
    name = display_name(caller.claims.model_extra or {})
    if store is None or name is None:
        return
    try:
        await store.remember_person(caller.subject, name)
    except DatabaseError:
        # A name is a convenience beside the write; the write itself reports any fault of the store it then meets.
        logger.warning("person_name_not_kept", exc_info=True)


async def names_for(request: Request, principals: Iterable[str]) -> dict[str, str]:
    """The kept names of ``principals``, for a read that already shows them; none from a service with no state store."""
    store: AnnotatorStore | None = getattr(request.app.state, "store", None)
    return {} if store is None else await store.names_of(principals)


#: Carried by every router whose doors record a person.
RECORDS_PERSON: Final = [Depends(record_person)]
