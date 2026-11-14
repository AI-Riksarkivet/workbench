"""DPoP proof verification for every request a workbench service serves (RFC 9449 § 4.3, § 7, § 11.1).

The browser's access token is bound to a key it holds (RFC 10017 § 6.1: a token kept in memory is still readable by a
script injected into the page), so presenting the token proves nothing on its own: each request carries a fresh proof
that the presenter holds the key the token is bound to. A proof is accepted only when every rule of § 4.3 holds:

* one ``DPoP`` header, a compact JWS with ``typ`` ``dpop+jwt``, an asymmetric ``alg`` from :data:`ALGORITHMS`, and a
  public ``jwk`` in its header that verifies its signature and carries no private part;
* ``htm`` is the request's method and ``htu`` its URL, with no query or fragment;
* ``iat`` is within the window around now;
* ``ath`` is the base64url SHA-256 of the access token it travels with;
* the key's RFC 7638 thumbprint is the token's ``cnf.jkt`` (§ 6.1), so a token bound to another key is refused;
* its ``jti`` has not been seen within the window (§ 11.1).

The JWS and JWK work is joserfc's (signature, key import, the RFC 7638 thumbprint); this module states the § 4.3
rules over it. No maintained Python library implements them (Authlib 1.9.5 has none; pydpop 0.2.1 is an alpha that
accepts ES256 alone).

THE REPLAY STORE IS PER PROCESS, AND THAT IS ITS STATED LIMIT: a proof replayed to another replica of the same service
within the window is not caught, because the workbench runs no store its replicas share. The window and the key binding
bound what such a replay can do: the replayer holds a proof for one method and URL, for at most the window, and no key
to make another. Within a process it is kept per person (`ReplayStore`), so no one can flood it to forget another's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OKPKey, RSAKey, import_key
from pydantic import BaseModel, ConfigDict, Field, ValidationError


#: The asymmetric algorithms a proof may use; `none` and the symmetric `HS*` are never among them (§ 4.3 rule 5).
ALGORITHMS: Final = frozenset({"ES256", "ES384", "ES512", "PS256", "PS384", "PS512", "RS256", "RS384", "RS512", "EdDSA"})
#: How far a proof's `iat` may lie from now, either way.
DEFAULT_WINDOW_SECONDS: Final = 60
#: The most proofs the replay store remembers per person, and the most people it remembers; past either, the oldest is
#: forgotten first (`ReplayStore`).
REPLAY_PER_PERSON: Final = 10_000
REPLAY_PEOPLE: Final = 10_000


class InvalidProofError(Exception):
    """The proof, or its binding to the access token, fails a rule of RFC 9449 § 4.3. The message names the rule."""


class _ProofClaims(BaseModel):
    model_config = ConfigDict(extra="allow")

    jti: str = Field(min_length=1, max_length=256)
    htm: str
    htu: str
    iat: int
    ath: str | None = None


def ath_of(access_token: str) -> str:
    """The `ath` a proof sent with ``access_token`` carries: base64url, unpadded, of its SHA-256."""
    return base64.urlsafe_b64encode(hashlib.sha256(access_token.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


#: The port a scheme implies, which RFC 3986 § 6.2.3 drops when a URL names it.
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


#: RFC 3986 § 2.3: the characters a percent-encoding of which is equivalent to the character itself.
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_ENCODED: Final = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalised_path(path: str) -> str:
    """``path`` after RFC 3986 § 6.2.2's syntax-based normalisation of percent-encoding: the hex digits of every
    triplet upper-cased (§ 6.2.2.1) and the triplets of unreserved characters decoded (§ 6.2.2.2). A reserved character
    stays as it was sent, encoded or not, since encoding one changes what the path says (§ 2.2)."""

    def triplet(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT_ENCODED.sub(triplet, path)


def _without_query(url: str) -> str:
    """``url`` in the form § 4.3 rule 9 compares: no query or fragment, the syntax-based normalisation of RFC 3986
    § 6.2.2 (the scheme and host lower-cased, the path's percent-encoding normalised by `_normalised_path`), and the
    scheme-based normalisation of § 6.2.3: the scheme's default port dropped, an empty path ``/``."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    if parts.username or parts.password:
        netloc = f"{parts.netloc.rsplit('@', 1)[0]}@{netloc}"
    return urlunsplit((scheme, netloc, _normalised_path(parts.path) or "/", "", ""))


class ReplayStore:
    """The `jti`s seen within the window, in this process, kept per person.

    PER PERSON, so no one can flood it to forget another's proof: an entry is recorded only for a proof that already
    passed every other rule, so a person fills only their own bucket, with proofs only their own key signs. Each bucket
    holds :data:`REPLAY_PER_PERSON` jtis and the store :data:`REPLAY_PEOPLE` people; past either the oldest is forgotten
    first. The limit that remains: a person whose own requests exceed the bucket within the window forgets their own
    oldest proofs, and a stolen one of those is replayable for the rest of its `iat` window; and with more than
    :data:`REPLAY_PEOPLE` people signing in within the window, the least recent person's bucket is forgotten.
    """

    def __init__(self, *, window_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_seconds
        self._clock = clock
        self._seen: OrderedDict[str, OrderedDict[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def first_use(self, person: str, jti: str) -> bool:
        """Record ``person``'s ``jti``; whether it was unseen within the window. A proof is good for twice the window at
        most (its `iat` either side of now)."""
        now = self._clock()
        with self._lock:
            bucket = self._seen.pop(person, None) or OrderedDict()
            self._seen[person] = bucket
            while len(self._seen) > REPLAY_PEOPLE:
                self._seen.popitem(last=False)
            while bucket and (next(iter(bucket.values())) <= now or len(bucket) >= REPLAY_PER_PERSON):
                bucket.popitem(last=False)
            if jti in bucket:
                return False
            bucket[jti] = now + 2 * self._window
            return True


class DpopVerifier:
    """Verify one request's proof against the access token it carries."""

    def __init__(self, *, window_seconds: int = DEFAULT_WINDOW_SECONDS, clock: Callable[[], float] = time.time) -> None:
        self._window = window_seconds
        self._clock = clock
        self.replays = ReplayStore(window_seconds=window_seconds)

    def verify(self, proof: str, *, method: str, url: str, access_token: str, jkt: str | None, person: str) -> None:
        """Accept the proof, or raise :class:`InvalidProofError` naming the rule it breaks.

        ``jkt`` is the access token's ``cnf.jkt``; a token that names no key is not DPoP-bound and is refused. ``person`` is
        the token's verified principal, whose replay bucket the `jti` is recorded in.
        """
        try:
            extracted = jws.extract_compact(proof.encode("ascii"))
        except (JoseError, ValueError, UnicodeError) as exc:
            raise InvalidProofError("the DPoP proof is not a compact JWS") from exc
        header = extracted.headers()
        if header.get("typ") != "dpop+jwt":
            raise InvalidProofError("the DPoP proof's typ is not dpop+jwt")
        alg = header.get("alg")
        if alg not in ALGORITHMS:
            raise InvalidProofError("the DPoP proof's alg is not an accepted asymmetric algorithm")
        if not isinstance(header.get("jwk"), dict):
            raise InvalidProofError("the DPoP proof carries no jwk")
        try:
            key = import_key(header["jwk"])
        except (JoseError, ValueError, TypeError) as exc:
            raise InvalidProofError("the DPoP proof's jwk is not a key") from exc
        if not isinstance(key, ECKey | RSAKey | OKPKey):
            raise InvalidProofError("the DPoP proof's jwk is not an asymmetric key")
        if key.is_private:
            raise InvalidProofError("the DPoP proof's jwk carries a private key")
        try:
            jws.deserialize_compact(proof, key, algorithms=[str(alg)])
        except (JoseError, ValueError) as exc:
            raise InvalidProofError("the DPoP proof's signature does not verify with its jwk") from exc
        try:
            claims = _ProofClaims.model_validate(json.loads(extracted.payload))
        except (ValidationError, ValueError) as exc:
            raise InvalidProofError("the DPoP proof lacks jti, htm, htu or iat") from exc
        if claims.htm != method.upper():
            raise InvalidProofError("the DPoP proof's htm is not the request's method")
        if _without_query(claims.htu) != _without_query(url):
            raise InvalidProofError("the DPoP proof's htu is not the request's URL")
        if abs(self._clock() - claims.iat) > self._window:
            raise InvalidProofError("the DPoP proof's iat is outside the accepted window")
        if claims.ath is None or not hmac.compare_digest(claims.ath, ath_of(access_token)):
            raise InvalidProofError("the DPoP proof's ath is not the hash of the access token")
        if not jkt or not hmac.compare_digest(key.thumbprint(), jkt):
            raise InvalidProofError("the access token is not bound to the DPoP proof's key")
        if not self.replays.first_use(person, claims.jti):
            raise InvalidProofError("the DPoP proof's jti was already used")
