"""OIDC ID-token verification wired directly on **PyJWT** (no python-jose / wrapper lib).

The cryptography is PyJWT's, not ours: ``PyJWKSet`` parses the provider's JWKS and ``jwt.decode`` does the
signature / issuer / audience / expiry math. What we add is the thin OIDC plumbing PyJWT does not: read the
provider's ``.well-known/openid-configuration`` for the issuer + JWKS URI and the key set itself, each by one bounded
read (`_read_document`: httpx, a total deadline, a byte cap, no redirect followed), and enforce a *local*
signing-algorithm allowlist before handing the token to ``jwt.decode`` (below). Provider-agnostic — works with
Keycloak, Dex, Okta, Auth0, Entra, or Google by setting the issuer + audience. (We do NOT hand-roll JWT crypto — see the don't-reinvent audit.)

Security posture (see CHANGELOG in the task notes):

* **Local algorithm allowlist.** We never trust the provider's advertised
  ``id_token_signing_alg_values_supported`` blindly. We intersect it with a
  configured allowlist of asymmetric algorithms and pass only that safe set to
  ``jwt.decode``. ``none`` and the symmetric ``HS*`` family are rejected to defeat
  alg-confusion (an attacker signing ``HS256`` with the public key as the secret).
* **Clock-skew leeway.** A small, configurable ``leeway`` absorbs clock drift
  between us and the IdP while we still *require* ``exp`` / ``iat`` / ``aud``.
* **HTTPS enforcement.** Both the issuer and its ``jwks_uri`` must be HTTPS unless
  ``allow_insecure`` is set, or the issuer is one of ``insecure_issuers`` (needed only for a local development
  provider over http, and declared issuer by issuer).
* **Multiple accepted issuers.** ``issuer`` may be a single string or a list; each
  is discovered independently and a token is verified against the issuer whose
  discovery document matches the token's ``iss`` claim.
* **Split-horizon discovery.** ``discovery_overrides`` maps a configured issuer to
  the base URL its discovery document is fetched from — the reverse-proxy IdP
  topology where tokens carry the *public* issuer (what the browser sees) but that
  URL is not reachable from inside the cluster. Only the fetch location changes:
  the discovery document's ``issuer`` must still equal the configured issuer, and
  tokens are still validated against it. A ``jwks_uri`` under the issuer is rebased
  onto the override so the key fetch stays in-cluster; HTTPS enforcement applies to
  the override URL under the same ``allow_insecure`` knob.
* **A credential for the fetch, when the issuer demands one.** A Kubernetes service-account issuer
  serves discovery and its key set only to a bearer and only from a private CA (measured on k3s
  2026-10-02: `--anonymous-auth=false` answers 401, the default trust store fails TLS). ``fetch_token_file``
  names a projected token re-read on every fetch, because the kubelet rotates it under the process;
  ``ca_file`` names the CA both fetches trust. ([[LH-220]])
* **One rule for how long a key is trusted, in every service, kept off the request path.** One background refresher
  per verifier reads each issuer's key set every `KEY_SET_MAX_AGE_SECONDS`; a request never fetches and never waits on
  another, and judges its token on the set read last while that is younger than `KEY_SET_STALE_LIMIT_SECONDS`, else
  answers 503 at once. The whole rule (a `kid` the issuer withdrew is refused at once), and its one bounded exception
  (a `kid` the set does not hold and was never seen withdrawn), is `OIDCVerifier`'s docstring.
* **Opaque failures, charged to their author.** A failure the presented token causes is a
  generic ``UnauthenticatedError``; one the provider or this deployment's configuration of it
  causes (discovery, the key set, a non-HTTPS URL, no algorithm in common) is a
  ``ProviderUnavailableError`` naming what failed. Neither leaks library names or crypto detail.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Annotated, Any, Final, NamedTuple
from urllib.parse import urlsplit

import httpx
import jwt
from lance_namespace import UnauthenticatedError
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

from service_kit.exceptions import ServiceUnavailableError


_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

#: The longest any one socket operation of a read of discovery or of the key set may block (connect, each read), the
#: same for both so neither is left to a library default free to drift on an upgrade (SKG-12), and never longer than
#: what is left of the read's deadline when it starts. A per-operation timeout alone does not bound a whole read (headers
#: or a body that drip a byte at a time): `_read_document`'s watchdog shuts the connection down at
#: `FETCH_DEADLINE_SECONDS`, whatever the read is blocked in.
HTTP_FETCH_TIMEOUT_SECONDS = 5.0

#: The longest one refresher attempt (discovery where due, then the key set) may take in all, after which it is
#: abandoned and counted failed. 5 s: both documents are a few kilobytes from an issuer on the cluster network, answered
#: in milliseconds when it is up, so an answer not come in 5 s is an issuer in trouble. It is also the longest a request
#: ever waits: one waiting for a forced refresh, or one sent to a verifier before its first attempt ended. The read
#: itself is cut at it from outside (`_read_document`'s watchdog shuts its connection down), so the thread of an
#: abandoned attempt ends within about a second of it when it was in the TLS handshake, the headers or the body. The
#: connect is bounded per address the name resolves to: at most what was left of the deadline for each, so a name with n
#: addresses that all stall can hold the thread up to n times that. A name lookup the resolver never answers is bounded
#: by nothing here; `ABANDONED_READS` caps the threads either leaves.
FETCH_DEADLINE_SECONDS = 5.0

#: The most bytes a discovery document or a key set may be: a read past it is cut and the connection closed. 1 MiB: an
#: issuer's documents are a few kilobytes (a key set of ten RSA-4096 keys is about 8 KiB), so a body past it is an issuer
#: in trouble or a hostile one, and it stays far below what a service's memory would notice.
MAX_DOCUMENT_BYTES: Final = 1 << 20

#: How many abandoned attempts per issuer may still be running before the refresher starts no other: an abandoned attempt
#: does not block the next one, so an issuer that hangs one connection is read again on a new one at the next attempt,
#: and an issuer that hangs every connection holds at most this many threads.
ABANDONED_READS: Final = 2

#: How often the refresher looks at whether an issuer is due, on a wall clock (ages are read on the verifier's clock):
#: what a forced refresh or `close` adds to a wait is at most this, since both wake it at once.
REFRESHER_TICK_SECONDS = 0.5

#: The least time between two key-set refetches forced by a token naming a `kid` the cached set does not hold, per
#: issuer. The `kid` is the caller's own text, unverified, so without a pace every anonymous request naming a novel one
#: buys a live fetch from the IdP (and, for a service-account issuer, the mirror's two from the API server), each free to
#: hold a threadpool worker for `HTTP_FETCH_TIMEOUT_SECONDS` ([[LH-431]]). Paced, a flood costs one fetch per interval,
#: and an honest rotation's first token is found by the first refetch that begins once the interval has passed. The same
#: interval as `lineage_kit.keys.KEY_REFRESH_INTERVAL_SECONDS`, which paces the same refetch for signed lineage events.
UNKNOWN_KID_REFRESH_INTERVAL_SECONDS: Final = 30.0

#: How many requests per issuer may wait at once for a forced refresh in flight (`OIDCVerifier._forced`). 8: a page's
#: parallel reads right after a person signed in again at a restarted issuer (the app fires a handful at once: a
#: descriptor, a page of rows, the signed media of a page, an atlas; measured, at most three reach one service at once)
#: fit, so all of them meet the read that finds the new key; and a flood of forged `kid`s parks at most 8 threads (of the
#: 40 a service's threadpool runs) for at most one deadline per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`. One past the cap
#: is told to come back (503), never refused.
FORCED_WAITERS: Final = 8

#: How long a key set the issuer served is believed before it is asked for again, counted from its fetch, the same for
#: every service. A key the issuer stops publishing (Dex on memory storage makes new keys at every restart; a key
#: withdrawn after a compromise) is refused by every service within this long after it left the set, plus one attempt:
#: the refresher reads the set again at this age, off the request path, while the issuer answers.
#: An honest rotation never meets it, since an issuer keeps a retired key published while tokens it signed are live.
#: Measured 2026-11-20 on the verifier this replaced: each process kept PyJWT's set for 300 s from its own last fetch,
#: so after Dex restarted the viewer refused a token the annotator kept accepting for minutes. The cost is one key-set
#: fetch a minute per issuer per process while requests arrive.
KEY_SET_MAX_AGE_SECONDS: Final = 60.0

#: How long past its fetch the last key set the issuer served is still used while the issuer cannot be reached (or
#: serves no usable set): a rolling restart of the issuer is not an outage of every service. Past it every token is
#: refused 503. It is the lifetime the replaced cache gave every set, so no key is trusted longer than it was; and a
#: token whose key is not in the set held is never accepted, whatever the issuer's state.
KEY_SET_STALE_LIMIT_SECONDS: Final = 300.0

#: How long after a failed attempt at an issuer's key set or discovery document the refresher tries again, per issuer
#: and process: an issuer that is down costs one read per interval whatever arrives, since requests read nothing.
#: Short enough that a pod which started while its issuer was briefly unreachable signs people in within 5 s of the
#: issuer answering again; `/readyz` and `/livez` read no token, so the pod stays ready meanwhile.
FETCH_RETRY_SECONDS: Final = 5.0

#: How many successful reads of an issuer's key set running must lack a `kid` that a read before them held, with no read
#: holding it between, before the `kid` is retired (`OIDCVerifier`). Two, not one: an issuer whose replicas behind one
#: address serve different sets (a rollout under way, a cache per node) answers one read without a key it still signs
#: with, and the next read may well hold it again. A key missing from two reads running is taken as withdrawn, which is
#: a judgement, not proof: an issuer that serves a set without a key it still signs with for two reads running has that
#: key's tokens refused until a read holds it again. Every successful read counts, the periodic one and a forced one
#: alike: each samples what the issuer publishes, whatever started it, and forced reads are paced at one per
#: `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` per issuer, so a caller adds at most that many samples. Counting the forced
#: read means a token of a rotated-out key meets one forced read after the first periodic read without it (refused 401
#: when it lacks the key), and every request after it is refused at once.
RETIRE_AFTER_READS: Final = 2

#: How long a `kid` stays retired (refused at once, `OIDCVerifier`) after the read that retired it. Nothing needs
#: remembering past the life of the last token the key signed, which no token outlives its `exp`; 24 h is the longest
#: default token lifetime of the providers the workbench meets (Dex's `idTokens` expiry; Keycloak, Okta, Entra and
#: Google mint for an hour or less). A token minted for longer whose key retired a day ago meets the unknown-`kid` rule
#: instead, which refuses it as surely, at the cost of one paced read.
RETIRED_KID_SECONDS: Final = 24 * 3600.0

#: The most retired `kid`s remembered per issuer, the oldest forgotten first. 32: an honest issuer retires a key or two
#: per rotation (Dex every 6 h by default, or at each restart on memory storage), so a day of them fits many times over;
#: an issuer that publishes and withdraws keys by the thousand costs no more memory than this. A forgotten `kid` meets
#: the unknown-`kid` rule, which is safe.
RETIRED_KIDS_PER_ISSUER: Final = 32

#: Asymmetric signing algorithms we are willing to accept. Symmetric (``HS*``) and
#: ``none`` are deliberately excluded — accepting them with a public verification key
#: enables the classic alg-confusion forgery. Callers may further narrow this set,
#: but can never widen it past what the provider also advertises.
DEFAULT_ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
)


class IDToken(BaseModel):
    """OIDC ID-token claims; ``extra='allow'`` keeps provider-specific fields accessible.

    Subclass this for provider-specific claims and validate against the subclass,
    e.g. for Okta::

        class OktaIDToken(IDToken):
            email: str
            groups: list[str] = []

    The required core claims (``iss``/``sub``/``aud``/``exp``/``iat``) are present on
    every conformant OIDC ID token; everything else is preserved via ``extra='allow'``.
    """

    model_config = ConfigDict(extra="allow")

    iss: str
    sub: str
    aud: str | list[str]
    exp: int
    iat: int


def _fetchable_url(url: str) -> str:
    """``url`` unchanged, if it parses and names an HTTP scheme; `urlsplit` raising on one that does not parse is part of the check."""
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("must be an http or https URL")
    return url


class _Discovery(BaseModel):
    """The subset of the provider's discovery document we rely on (boundary-validated)."""

    issuer: str
    # Checked here, where a bad value is the document's fault: `_require_https` and the key-set read parse it
    # outside any mapping, and raise `ValueError` / an httpx error for one that does not parse or is not HTTP.
    jwks_uri: Annotated[str, AfterValidator(_fetchable_url)]
    id_token_signing_alg_values_supported: list[str] = []


class _Provider(NamedTuple):
    """A resolved provider: its discovery doc, where its key set is read from, and the safe algorithm set.

    An internal value bundle (not boundary input), so a ``NamedTuple`` is the right
    container — no per-construction validation, just an immutable typed triple.
    """

    spec: _Discovery
    jwks_uri: str
    algorithms: list[str]


log = logging.getLogger(__name__)


class _FetchCredential:
    """The bearer and CA an issuer's fetches carry; neither is set for a public IdP.

    The token is read on every fetch, never kept: a projected token is rotated by the kubelet at 80% of its
    lifetime, so a copy taken at construction stops working while the process runs.
    """

    def __init__(self, token_file: str | None, ca_file: str | None) -> None:
        self._token_file = token_file
        self.ssl_context: ssl.SSLContext | None = ssl.create_default_context(cafile=ca_file) if ca_file else None

    def headers(self) -> dict[str, str]:
        if self._token_file is None:
            return {}
        token = Path(self._token_file).read_text().strip()
        if not token:
            raise OSError(f"the fetch credential at {self._token_file} is empty")
        return {"Authorization": f"Bearer {token}"}

    def verify(self) -> ssl.SSLContext | bool:
        return self.ssl_context if self.ssl_context is not None else True


class _ReadCut(OSError):
    """A read of an issuer's document stopped by this side: past its deadline, or past `MAX_DOCUMENT_BYTES`. An
    ``OSError``, so it is charged to the provider wherever a read that could not complete is."""


class _Watchdog:
    """Shuts down every connection a read opened once its deadline passes, from outside the read.

    A timeout per socket operation cannot bound a peer that sends one header line, one TLS record or one body byte at a
    time, each inside it; so the deadline is enforced by this timer rather than by anything the peer does. Each
    connection is reported by httpcore's ``trace`` extension as soon as its TCP connect completes, before any TLS or
    HTTP byte is exchanged; the watchdog keeps a duplicate of its socket (`socket.dup`: the same connection, a file
    descriptor of its own, so it stays usable after TLS takes the original socket object over) and, at the deadline,
    shuts the connection down (``SHUT_RDWR``), which wakes a read blocked in the TLS handshake, the status line, the
    headers or the body with an error in the reading thread. A connection that completes after the deadline is shut
    down at once. The connect itself is bounded by its timeout (at most what is left of the deadline, per address the
    name resolves to); a name lookup is bounded by nothing here (`ABANDONED_READS`).
    """

    def __init__(self, deadline: float) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.cut = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._cut)
        self._timer.daemon = True

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._timer.cancel()
        with self._lock:
            for sock in self._sockets:
                sock.close()
            self._sockets.clear()

    def trace(self, event: str, info: dict[str, Any]) -> None:
        """httpcore's trace callback: keeps each connection the moment it is made."""
        if not event.endswith(".connect_tcp.complete"):
            return
        raw = info["return_value"].get_extra_info("socket")
        if not isinstance(raw, socket.socket):
            return
        held = raw.dup()
        with self._lock:
            self._sockets.append(held)
            if self.cut:
                self._shut(held)

    def _cut(self) -> None:
        with self._lock:
            self.cut = True
            for sock in self._sockets:
                self._shut(sock)

    @staticmethod
    def _shut(sock: socket.socket) -> None:
        # A connection already closed by its reader has nothing left to wake.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _read_document(url: str, credential: _FetchCredential, deadline: float) -> bytes:
    """The body ``url`` answers with a 2xx, read whole by ``deadline`` (`time.monotonic`) and within
    `MAX_DOCUMENT_BYTES`, or an error; on either cut the connection is closed.

    The deadline is enforced from outside the read (`_Watchdog`), so nothing the peer does extends it: a thread reading
    ends within about a second of the deadline in the TLS handshake, the status line and headers, and the body. The
    connect is bounded per address the name resolves to (at most what was left of the deadline for each), and a name
    lookup by nothing here. Streamed raw, so the cap is checked as each chunk arrives and is counted on the bytes the peer
    sent: ``Accept-Encoding: identity`` is asked, and a body sent compressed anyway is never inflated (it then fails to
    parse, the provider's fault). Each socket operation is given at most what is left of the deadline (capped at
    `HTTP_FETCH_TIMEOUT_SECONDS`). TLS is verified as the credential says (its CA, else the default trust store), and
    proxies are taken from the environment as httpx does; no redirect is followed, so the document is read from the URL
    the configuration or the discovery document named and from nowhere else (a 3xx is an error). The fetch credential
    is read afresh on every call: the kubelet rotates a projected token under the process.
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise _ReadCut(f"no time was left to read {url}")
    timeout = min(HTTP_FETCH_TIMEOUT_SECONDS, left)
    headers = credential.headers() | {"Accept-Encoding": "identity"}
    with _Watchdog(deadline) as watchdog:
        try:
            with (
                httpx.Client(timeout=timeout, verify=credential.verify(), follow_redirects=False) as client,
                client.stream("GET", url, headers=headers, extensions={"trace": watchdog.trace}) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_raw():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise _ReadCut(f"{url} answered more than {MAX_DOCUMENT_BYTES} bytes")
                    if time.monotonic() > deadline:
                        raise _ReadCut(f"{url} did not finish answering within its deadline")
                return bytes(body)
        except httpx.HTTPError as exc:
            if watchdog.cut:
                raise _ReadCut(f"{url} did not finish answering within its deadline") from exc
            raise


class _RefreshPace:
    """Admits one unknown-kid refetch per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` for one issuer.

    The pace is counted from when a refetch BEGAN, and the moment is claimed under the lock before the fetch runs, outside
    it: a burst arriving while a refetch is in flight is declined at once instead of queueing behind a slow IdP, so it
    holds no worker for the fetch budget. A refetch that fails counts like one that succeeds, since a hung IdP is exactly
    when another attempt costs most.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._began: float | None = None

    def admit(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._began is not None and now - self._began < UNKNOWN_KID_REFRESH_INTERVAL_SECONDS:
                return False
            self._began = now
            return True

    def left(self) -> float:
        """How long until the pace admits another refetch: 0 when it would now."""
        with self._lock:
            return 0.0 if self._began is None else max(0.0, self._began + UNKNOWN_KID_REFRESH_INTERVAL_SECONDS - self._clock())


class _Keys(NamedTuple):
    """A key set an issuer served: where from, its signing keys, and when its fetch ENDED. Immutable, replaced whole."""

    uri: str
    keys: tuple[jwt.PyJWK, ...]
    fetched_at: float


class _Issuer:
    """What the refresher knows of one configured issuer, read and replaced under the verifier's lock.

    The discovery document read last and when; the key set read last (`_Keys`); why the last attempt failed and the
    moment before which the refresher does not try again; a forced refresh asked for by a token naming a `kid` the set
    does not hold (``forced_asked`` counts the asks, ``forced_started`` the asks the last attempt to START covered,
    recorded under the lock as it starts, ``forced_served`` the asks an ended attempt answered, ``forced_published`` the
    asks an ended attempt answered with a key set it read, and ``forced_unread`` says the last forced ask got none, so
    the refresher asks again at the retry pace); how many requests wait for it; how many attempts
    have ended; the fetch threads of abandoned attempts that still run; the `kid`s absent from the latest reads, each with
    how many reads running lacked it; and the retired `kid`s, each with when the read that retired it ended, oldest
    first (`_retire`). Not a model: mutable state behind a lock, never boundary input.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self.provider: _Provider | None = None
        self.discovered_at = 0.0
        self.discovery_retry_at = 0.0
        self.keys: _Keys | None = None
        self.failure = "The OIDC issuer has not been read yet"
        self.retry_at = 0.0
        self.forced_asked = 0
        self.forced_started = 0
        self.forced_served = 0
        self.forced_published = 0
        self.forced_unread = False
        self.forced_waiters = 0
        self.attempts = 0
        self.abandoned: list[threading.Thread] = []
        self.pace = _RefreshPace(clock)
        self.retired: dict[str, float] = {}
        self.absent: dict[str, int] = {}


def _retire(state: _Issuer, before: _Keys | None, after: _Keys, now: float) -> None:
    """Record what publishing ``after`` in place of ``before`` retires. A `kid` ``before`` held that ``after`` does not is
    absent from one read; one absent from the reads before ``after`` and from ``after`` too is absent from one more; at
    `RETIRE_AFTER_READS` it is retired, at ``now``. A `kid` ``after`` holds is neither absent nor retired, however it
    was before. Only sets the issuer served reach here, so only `kid`s the issuer itself published can retire; with no
    ``before`` (the first set this process read) none is absent. A `kid` retired `RETIRED_KID_SECONDS` before is
    forgotten (by this read: the snapshot is at most `KEY_SET_STALE_LIMIT_SECONDS` old when a request consults it), and
    past `RETIRED_KIDS_PER_ISSUER` the oldest, of the retired and of the absent alike."""
    current = {key.key_id for key in after.keys if key.key_id is not None}
    for kid in current:
        state.retired.pop(kid, None)
        state.absent.pop(kid, None)
    absent = {kid: reads + 1 for kid, reads in state.absent.items()}
    for key in () if before is None else before.keys:
        if key.key_id is not None and key.key_id not in current:
            absent[key.key_id] = 1
    state.absent = {}
    for kid, reads in absent.items():
        if reads >= RETIRE_AFTER_READS:
            state.retired[kid] = now
        else:
            state.absent[kid] = reads
    while len(state.absent) > RETIRED_KIDS_PER_ISSUER:
        del state.absent[next(iter(state.absent))]
    for kid, at in list(state.retired.items()):
        if now - at >= RETIRED_KID_SECONDS:
            del state.retired[kid]
    while len(state.retired) > RETIRED_KIDS_PER_ISSUER:
        del state.retired[next(iter(state.retired))]


class ProviderUnavailableError(ServiceUnavailableError):
    """The provider could not be used — its documents or this deployment's configuration of it, never the token.

    A fleet `ServiceUnavailableError`, so the fleet doors render it with the URL or setting that failed. A
    door that answers in the Lance taxonomy catches this type and raises its own `lance_namespace` 503
    instead: that body carries the spec's `code` and redacts the message. Named here so such a door can
    catch it without importing the fleet taxonomy.
    """


def _require_https(url: str, *, label: str, allow_insecure: bool) -> None:
    """Reject non-HTTPS ``url`` unless ``allow_insecure`` is set (dev-only escape hatch)."""
    if allow_insecure:
        return
    if urlsplit(url).scheme != "https":
        # A configuration fault, never the caller's: the URL is this deployment's setting or the provider's
        # discovery document, and no token is read. The log line names which one, because a Lance door's 503
        # redacts the message.
        #
        # The ISSUER case is caught earlier, at settings construction (`GovernedAuthSettings`), where a
        # misconfiguration belongs. This stays for the discovery override, and for `jwks_uri`, which
        # comes from discovery and so cannot be seen until verify time.
        log.warning("oidc_insecure_url", extra={"label": label, "scheme": urlsplit(url).scheme})
        raise ProviderUnavailableError(f"OIDC {label} must use HTTPS (set WORKBENCH_OIDC_ALLOW_INSECURE=true for dev IdPs)")


class OIDCVerifier:
    """Verify ID tokens against one or more providers, whose documents ONE background refresher keeps.

    THE WHOLE RULE. A request never fetches and never waits on another request:

    * The refresher (one daemon thread per verifier, started by `warm` at the app's startup, stopped by `close`, which
      it heeds between issuers and while it waits for an attempt, and started again by the next request if it is not
      running) reads each issuer's discovery document every ``cache_ttl`` and its key set every
      `KEY_SET_MAX_AGE_SECONDS`, tries a failed read again after `FETCH_RETRY_SECONDS`, gives each attempt
      `FETCH_DEADLINE_SECONDS` in all (each read is cut at it from outside, `_read_document`'s watchdog, and at
      `MAX_DOCUMENT_BYTES`, so an abandoned attempt's thread ends within about a second of it in the TLS handshake, the
      headers or the body; the connect is bounded per address the name resolves to, at most what was left of the
      deadline for each, and a name lookup by nothing here; an attempt past it is abandoned and counted failed, and the
      next one starts on a new connection, with at most `ABANDONED_READS` abandoned ones running per issuer), outlives
      anything an attempt raises, and publishes a good key set by replacing one immutable snapshot (`_Keys`), its age
      counted from the fetch's END. An abandoned attempt's outcome is never read.
    * A request reads the snapshot: one younger than `KEY_SET_STALE_LIMIT_SECONDS` is the set its token is judged on;
      none, or an older one, is a 503 at once with ``Retry-After``. So every service drops a key the issuer stopped
      publishing within one refresh (60 s), and trusts none past 300 s from its fetch while the issuer is away.
    * A token naming a retired `kid` is refused 401 at once, with no read asked, no wait and no pace spent. A `kid` is
      retired when a key set this verifier read held it and the `RETIRE_AFTER_READS` successful reads after it, periodic
      or forced, all lack it: taken as the issuer withdrawing the key (Dex restarting on memory storage), so a session it
      signed is over. One read without it is not enough, since replicas of one issuer can serve different sets; an
      issuer that serves a set without a key it still signs with for that many reads running has that key's tokens
      refused until a read holds it again. Only successful reads count, only `kid`s the issuer published can be
      retired, each issuer's are its own, the first set a process reads retires nothing, a `kid` the current snapshot
      holds is never refused for it (an issuer that publishes a key again), and a `kid` is forgotten after
      `RETIRED_KID_SECONDS` or past `RETIRED_KIDS_PER_ISSUER`, when it meets the exception below, as does one a single
      read lacked so far.
    * One bounded exception: a token naming a `kid` the snapshot does not hold and that is not retired (a rotation the
      refresher has not read yet, or a key withdrawn before this process first read the set) asks for ONE forced
      refresh, at most one per issuer per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`; it and every such request arriving
      while that refresh is in flight, up to `FORCED_WAITERS` per issuer, wait for its outcome for at most one
      deadline, and is let in when the set that refresh read holds its `kid`. The token is refused 401 only when a
      read of the issuer that STARTED after it asked read a key set that does not hold its `kid` (an attempt records
      the asks it covers as it starts, `forced_started`): a read that began before the request asked may have read the
      set as it was before the key was published. Every other outcome is the provider's, a 503 with
      ``Retry-After`` (`_come_back`): the forced read failed or ran out (it is asked again at `FETCH_RETRY_SECONDS`), the
      cap was reached (one deadline), the read it waited for began before it asked and lacks its `kid`, or the pace is
      spent with no read in flight (until the pace or the periodic read allows one). So a forged `kid` ends nobody's
      session, and an honest new key is let in once the issuer is read. A request to a verifier no refresh
      has ended for yet (one never warmed) waits for that first attempt likewise, bounded the same way.
    """

    def __init__(
        self,
        issuer: str | list[str],
        audience: str,
        cache_ttl: int,
        *,
        allowed_algorithms: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_ALGORITHMS,
        leeway: int = 60,
        allow_insecure: bool = False,
        insecure_issuers: Collection[str] = (),
        discovery_overrides: dict[str, str] | None = None,
        fetch_token_file: str | None = None,
        ca_file: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        issuers = [issuer] if isinstance(issuer, str) else list(issuer)
        if not issuers:
            raise ValueError("OIDCVerifier requires at least one issuer")
        # Normalise (strip trailing slash) and de-duplicate while preserving order.
        self._issuers = list(dict.fromkeys(iss.rstrip("/") for iss in issuers))
        # Split-horizon fetch locations, keyed by the normalized configured issuer (see the
        # module docstring): only where discovery/JWKS are FETCHED — never what tokens carry.
        self._discovery_overrides = {iss.rstrip("/"): url.rstrip("/") for iss, url in (discovery_overrides or {}).items()}
        self._audience = audience
        self._ttl = cache_ttl
        self._clock = clock
        # Keyed by configured issuer, so the state is bounded by the configuration rather than by anything a token says.
        self._state = {iss: _Issuer(clock) for iss in self._issuers}
        # Guards every `_Issuer`, held only to read or replace its fields, never across a fetch; `_changed` tells a
        # request waiting for an attempt (the bounded exception) that one ended.
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._refresher: threading.Thread | None = None
        self._leeway = leeway
        self._allow_insecure = allow_insecure
        self._insecure_issuers = frozenset(iss.rstrip("/") for iss in insecure_issuers)
        self._credential = _FetchCredential(fetch_token_file, ca_file)
        # Keep the configured allowlist as an ordered, de-duplicated set of upper-cased
        # algorithm names so the intersection with the provider is deterministic.
        self._allowed = list(dict.fromkeys(alg.upper() for alg in allowed_algorithms))
        if not self._allowed:
            raise ValueError("OIDCVerifier requires a non-empty algorithm allowlist")

    def _safe_algorithms(self, advertised: list[str]) -> list[str]:
        """Intersect the provider's advertised algorithms with our local allowlist.

        Order follows our allowlist (our preference), and the result excludes anything
        not asymmetric. An empty intersection means we cannot safely verify against this
        provider, and the caller refuses it as the provider's fault.
        """
        advertised_upper = {alg.upper() for alg in advertised}
        # If the provider advertises nothing, fall back to our allowlist (RFC 8414 makes
        # the field OPTIONAL; many IdPs still only sign with RS256). This is still safe:
        # PyJWT enforces that the token's actual ``alg`` is in this asymmetric set.
        if not advertised_upper:
            return list(self._allowed)
        return [alg for alg in self._allowed if alg in advertised_upper]

    def _discover(self, configured_issuer: str, deadline: float) -> _Provider:
        """Read ``configured_issuer``'s discovery document now; the refresher's to call, never a request's.

        Raises ``ProviderUnavailableError`` naming what failed: the URL that could not be read, or how the document
        disagrees with this deployment's configuration.
        """
        # Split-horizon: fetch discovery from the override when one is configured for this
        # issuer; the issuer STRING (and every token check against it) is unchanged.
        override = self._discovery_overrides.get(configured_issuer)
        discovery_base = configured_issuer if override is None else override
        _require_https(
            discovery_base + _DISCOVERY_SUFFIX,
            label="issuer" if override is None else "discovery override",
            allow_insecure=self._allow_insecure or configured_issuer in self._insecure_issuers,
        )
        # OURS, NOT THE CALLER'S. Everything from here to the mismatch check below is a statement
        # about this deployment's configuration and the issuer it points at — no token is read and none
        # plays a part. They are `ProviderUnavailableError`s: what a request meets while they stand is a
        # 503, as `deps.py` answers for a verifier it does not have, because it is the same fact.
        try:
            # Parsed by the model rather than by `json.loads`: a body that is not JSON at all (a proxy's sign-in
            # page) then lands in the same `ValidationError` as one of the wrong shape, instead of a
            # `json.JSONDecodeError` that no branch below classifies.
            spec = _Discovery.model_validate_json(_read_document(f"{discovery_base}{_DISCOVERY_SUFFIX}", self._credential, deadline))
        except (httpx.HTTPError, OSError) as exc:
            # OSError is the fetch credential failing to read: still this deployment's fault, never the token's.
            # The message names the LOCATION rather than the exception: an operator reading a 503
            # needs to know which URL this deployment could not use, and a split-horizon override is
            # precisely the setting most likely to be the one that is wrong.
            log.warning("oidc_discovery_unreachable", extra={"discovery_base": discovery_base, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC issuer could not be reached at {discovery_base}{_DISCOVERY_SUFFIX}") from exc
        except ValidationError as exc:
            log.warning("oidc_discovery_malformed", extra={"discovery_base": discovery_base, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC discovery document at {discovery_base}{_DISCOVERY_SUFFIX} is not a discovery document") from exc

        # The discovery document's own ``issuer`` is authoritative for token validation;
        # it must match what we configured (defends against a tampered discovery doc).
        #
        # A 503, not a 401, and that is the deliberate half of this change: the check compares the
        # DOCUMENT against this deployment's configuration, so a caller whose bearer is perfectly
        # good is told their credential is bad. The honest statement is that this service cannot
        # currently trust its own issuer. `_provider_for`'s "Unrecognized token issuer" stays a 401 —
        # that one IS about the token.
        if spec.issuer.rstrip("/") != configured_issuer:
            log.warning("oidc_discovery_issuer_mismatch", extra={"configured": configured_issuer, "advertised": spec.issuer})
            raise ProviderUnavailableError("The OIDC discovery document advertises a different issuer than this deployment is configured for")

        # A jwks_uri the provider advertises under its (public) issuer must be fetched from
        # the same split-horizon location as discovery; anything not under the issuer is a
        # provider hosting keys elsewhere and is used as advertised.
        jwks_uri = spec.jwks_uri
        if override is not None and (jwks_uri == configured_issuer or jwks_uri.startswith(configured_issuer + "/")):
            jwks_uri = override + jwks_uri[len(configured_issuer) :]
        _require_https(jwks_uri, label="jwks_uri", allow_insecure=self._allow_insecure or configured_issuer in self._insecure_issuers)
        algorithms = self._safe_algorithms(spec.id_token_signing_alg_values_supported)
        if not algorithms:
            # The provider's list against this deployment's allowlist; the token's own `alg` is not read.
            log.warning("oidc_no_common_algorithm", extra={"advertised": spec.id_token_signing_alg_values_supported, "allowed": self._allowed})
            raise ProviderUnavailableError("No mutually-supported OIDC signing algorithm")

        return _Provider(spec=spec, jwks_uri=jwks_uri, algorithms=algorithms)

    # ── the refresher ──────────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the refresher, or start it again when it is not running (an unforeseen error ended it)."""
        with self._lock:
            if self._stop.is_set() or (self._refresher is not None and self._refresher.is_alive()):
                return
            self._refresher = threading.Thread(target=self._run, name="oidc-refresher", daemon=True)
            self._refresher.start()

    def close(self) -> None:
        """Stop the refresher and wait for it, at most one deadline: what an app's shutdown calls."""
        self._stop.set()
        self._wake.set()
        refresher = self._refresher
        if refresher is not None:
            refresher.join(FETCH_DEADLINE_SECONDS + 1)

    def _run(self) -> None:
        """Each tick, refresh every issuer that is due. Nothing an attempt raises ends the loop but a process exit."""
        while not self._stop.is_set():
            try:
                for configured in self._issuers:
                    # Heeded between issuers, so `close` is not held by the issuers still due after one that hung.
                    if self._stop.is_set():
                        break
                    if self._due(configured):
                        self._refresh(configured)
            except SystemExit:
                raise
            # The refresher must outlive anything a refresh raises, BaseException-derived ones included: a refresher
            # that died would leave every request on a set that only ages.
            except BaseException as exc:
                log.error("oidc_refresher_error", extra={"error": repr(exc)})
            self._wake.wait(REFRESHER_TICK_SECONDS)
            self._wake.clear()

    def _due(self, configured: str) -> bool:
        """Whether ``configured`` needs an attempt now: a forced refresh asked for, or a document past its age and no
        failed attempt within `FETCH_RETRY_SECONDS`."""
        with self._lock:
            state = self._state[configured]
            if state.forced_asked > state.forced_served:
                return True
            now = self._clock()
            if now < state.retry_at:
                return False
            # A forced ask the last attempt could not answer with a key set is asked again at the retry pace, so a
            # request told to come back finds the key that a read of the issuer would hold.
            if state.forced_unread:
                return True
            discovery_due = state.provider is None or (now - state.discovered_at >= self._ttl and now >= state.discovery_retry_at)
            return discovery_due or state.keys is None or now - state.keys.fetched_at >= KEY_SET_MAX_AGE_SECONDS

    def _refresh(self, configured: str) -> None:
        """One attempt for ``configured``: its discovery document where due, then its key set, in a thread of its own
        given `FETCH_DEADLINE_SECONDS` in all, its reads bounded by the same deadline (`_read_document`). An attempt past
        it, or one `close` interrupts, is abandoned and counted failed; it does not block the next attempt, which reads
        on a new connection, unless `ABANDONED_READS` of this issuer's still run. Each attempt has an outcome list of its
        own, read only here and only for the attempt waited for: whatever an abandoned one appends later is never read."""
        with self._lock:
            state = self._state[configured]
            forced = state.forced_asked
            # The generation this attempt starts for, recorded as it starts: a request that asks after this joins a
            # read that began before it, and may not take what it finds as evidence against its `kid` (`_forced`).
            state.forced_started = forced
            state.abandoned = [thread for thread in state.abandoned if thread.is_alive()]
            crowded = len(state.abandoned) >= ABANDONED_READS
            provider = state.provider
            now = self._clock()
            rediscover = provider is None or (now - state.discovered_at >= self._ttl and now >= state.discovery_retry_at)
            # The key set is read when it is due (or forced, or never read); an attempt for discovery alone keeps it.
            keys_due = state.keys is None or forced > state.forced_served or state.forced_unread or now - state.keys.fetched_at >= KEY_SET_MAX_AGE_SECONDS
            held = None if keys_due or state.keys is None else state.keys.uri
        outcome: list[tuple[_Provider | None, list[jwt.PyJWK] | None, str | None]] = []
        if crowded:
            failure: str | None = f"The OIDC issuer's last {ABANDONED_READS} reads have not ended"
        else:
            deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
            worker = threading.Thread(target=self._attempt, args=(configured, provider, rediscover, held, deadline, outcome), name="oidc-fetch", daemon=True)
            worker.start()
            # Waited for in ticks, so `close` is heeded while an attempt runs rather than once its deadline passes.
            while worker.is_alive() and not self._stop.is_set() and (left := deadline - time.monotonic()) > 0:
                worker.join(min(left, REFRESHER_TICK_SECONDS))
            if worker.is_alive():
                with self._lock:
                    state.abandoned.append(worker)
            failure = None if outcome else f"The OIDC issuer did not answer within {FETCH_DEADLINE_SECONDS:g} s"
        # Read once, now: an abandoned attempt may still append, and that is never looked at.
        found, keys, failed = outcome[0] if outcome else (None, None, failure)
        with self._lock:
            now = self._clock()
            if found is not None and found is not provider:
                state.provider, state.discovered_at, state.discovery_retry_at = found, now, 0.0
            elif rediscover and provider is not None:
                # A re-read of discovery that failed keeps the documents read before, tried again at the pace.
                state.discovery_retry_at = now + FETCH_RETRY_SECONDS
            if keys is not None and found is not None:
                published = _Keys(uri=found.jwks_uri, keys=tuple(keys), fetched_at=now)
                _retire(state, state.keys, published, now)
                state.keys = published
                state.failure, state.retry_at = "", 0.0
                state.forced_published, state.forced_unread = max(state.forced_published, forced), False
            elif outcome and failed is None:
                # Discovery alone was due and was read; the key set held stands.
                state.failure, state.retry_at = "", 0.0
            else:
                state.failure, state.retry_at = failed or "The OIDC key set could not be read", now + FETCH_RETRY_SECONDS
                state.forced_unread = state.forced_unread or forced > state.forced_served
            state.forced_served = max(state.forced_served, forced)
            state.attempts += 1
            self._changed.notify_all()

    def _attempt(
        self,
        configured: str,
        provider: _Provider | None,
        rediscover: bool,
        held: str | None,
        deadline: float,
        outcome: list[tuple[_Provider | None, list[jwt.PyJWK] | None, str | None]],
    ) -> None:
        """The reads of one attempt, in the fetch thread, both by ``deadline``: its result, or why there is none, put in
        ``outcome``. The key set is read unless one is ``held`` (not due) from the location the discovery document still
        names."""
        try:
            if rediscover or provider is None:
                try:
                    provider = self._discover(configured, deadline)
                except ProviderUnavailableError:
                    # A re-read that fails keeps the documents read before; with none read, it is the attempt's answer.
                    if provider is None:
                        raise
            keys = None if held == provider.jwks_uri else self._fetch_keys(provider, deadline)
            outcome.append((provider, keys, None))
        except ProviderUnavailableError as exc:
            outcome.append((provider, None, str(exc)))
        # Whatever else an attempt raises (BaseException-derived ones too) is a failed attempt, never a dead refresher.
        except BaseException as exc:
            log.error("oidc_refresh_error", extra={"issuer": configured, "error": repr(exc)})
            outcome.append((provider, None, "The OIDC issuer could not be read"))

    def warm(self) -> list[tuple[str, str]]:
        """Start the refresher, wait for its first attempt at every configured issuer, and report the ones that failed.

        THE CONSTRUCTOR PERFORMS NO I/O, so a verifier that was merely built proves nothing about the
        IdP it points at. Without this call a wrong issuer, a wrong split-horizon override or an unreachable
        IdP surfaces as failed user traffic, long after the pod has reported healthy.

        IT REPORTS, IT DOES NOT DECIDE, and that is the load-bearing half. Returning the failures
        instead of raising keeps the decision with the caller, and today no caller treats an
        unreachable IdP as fatal — deliberately. Readiness that gates on a downstream dependency turns
        one IdP blip into every governed pod leaving its Service endpoints at once, which is strictly
        worse than the 503 the door answers on its own. The pod stays up and says what is wrong.

        Each entry is ``(configured_issuer, reason)``. The issuer is carried because a split-horizon
        deployment fetches from an override, so the URL that failed is not the one an operator set as
        the issuer — and that override is the setting most likely to be the wrong one.

        An issuer that is down at start leaves the pod ready (`/readyz` reads no token); its tokens are refused 503
        until the refresher's first success, with no request having fetched anything.
        """
        self.start()
        failures: list[tuple[str, str]] = []
        for configured in self._issuers:
            with self._lock:
                state = self._state[configured]
                self._changed.wait_for(lambda state=state: state.attempts > 0, timeout=FETCH_DEADLINE_SECONDS + 1)
                if state.keys is None:
                    failures.append((configured, state.failure))
        return failures

    def _issuer_of(self, token: str) -> str:
        """The configured issuer the token's (still unverified) ``iss`` names; verification then proves it. A token
        naming no configured issuer is refused here, with one issuer as with several."""
        try:
            claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        for configured in self._issuers:
            if isinstance(claimed, str) and claimed.rstrip("/") == configured:
                return configured
        raise UnauthenticatedError("Unrecognized token issuer")

    def _fetch_keys(self, provider: _Provider, deadline: float) -> list[jwt.PyJWK]:
        """The provider's signing keys as it serves them now; every failure to produce them is the PROVIDER's, never the token's.

        The failures are classified by AUTHOR, not by type: every input below is the provider's response, so whatever
        is raised is the provider's. Two kinds:

        * the set could not be read — an httpx error (a connection closed before the status line or reset mid-body, a
          body cut short, a non-2xx answer) or an ``OSError`` (`_ReadCut`: past the deadline or the byte cap; the fetch
          credential failing to read);
        * anything else: the set arrived and holds no usable signing key. Measured on pyjwt 2.13.0 that is
          `PyJWTError`, `ValueError`, `AttributeError`, `TypeError` and `RecursionError`, but it is caught
          whole: PyJWK hands provider values to `cryptography`, whose exceptions are not all `ValueError`s
          (`UnsupportedAlgorithm` subclasses `Exception` alone), and a type left out of a named tuple would
          answer 500 and be audited `invalid_token` against the caller.

        The keys kept are PyJWT's signing keys (`PyJWKClient.get_signing_keys`' filter): ``use`` is ``sig`` or unset,
        and a ``kid`` is named. Called by the refresher alone. An unusable set is never published (only what this
        returns is), so the provider's fix is believed at the refresher's next attempt.
        """
        uri = provider.jwks_uri
        try:
            body = _read_document(uri, self._credential, deadline)
        except (httpx.HTTPError, OSError) as exc:
            log.warning("oidc_jwks_unreachable", extra={"jwks_uri": uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set could not be reached at {uri}") from exc
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise TypeError("the key set is not a JSON object")
            keys = [key for key in jwt.PyJWKSet.from_dict(data).keys if key.public_key_use in ("sig", None) and key.key_id]
            if not keys:
                raise ValueError("the key set holds no signing key")
            return keys
        # Classified by author, not by type — see this docstring.
        except Exception as exc:
            log.warning("oidc_jwks_malformed", extra={"jwks_uri": uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set at {uri} holds no usable signing key") from exc

    # ── a request ──────────────────────────────────────────────────────────────────────────────────

    def _snapshot(self, configured: str) -> tuple[_Provider | None, _Keys | None, str]:
        """What a token of ``configured`` is judged on: the documents the refresher read last, the key set if it is
        younger than `KEY_SET_STALE_LIMIT_SECONDS` from its fetch (else None), and why there is none.

        Before the refresher has ended any attempt (a verifier nobody warmed), the request waits for that first one,
        at most one deadline: there is nothing else to judge it on.
        """
        self.start()
        with self._lock:
            state = self._state[configured]
            if state.attempts == 0:
                self._changed.wait_for(lambda: state.attempts > 0, timeout=FETCH_DEADLINE_SECONDS + 1)
            provider, keys = state.provider, state.keys
            if keys is not None and (provider is None or keys.uri != provider.jwks_uri or self._clock() - keys.fetched_at >= KEY_SET_STALE_LIMIT_SECONDS):
                keys = None
            return provider, keys, state.failure or "The OIDC key set is older than this deployment trusts"

    @staticmethod
    def _unavailable(failure: str, retry_after: float = FETCH_RETRY_SECONDS) -> ProviderUnavailableError:
        """The 503 a request meets at once while the issuer's documents are not usable: it says when to come back, in
        whole seconds and never less than one."""
        return ProviderUnavailableError(failure, headers={"Retry-After": str(max(1, math.ceil(retry_after)))})

    def _come_back(self, state: _Issuer) -> float:
        """When a token whose `kid` the set does not hold may next meet a read of the issuer, with the pace spent and
        none in flight: the soonest of the pace admitting a forced one, the refresher's periodic read, and, while a
        forced ask went unanswered, the refresher asking it again (`FETCH_RETRY_SECONDS` after it failed); never past
        one interval (`UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`)."""
        now = self._clock()
        soonest = min(state.pace.left(), KEY_SET_MAX_AGE_SECONDS if state.keys is None else state.keys.fetched_at + KEY_SET_MAX_AGE_SECONDS - now)
        if state.forced_unread:
            soonest = min(soonest, state.retry_at - now)
        return min(UNKNOWN_KID_REFRESH_INTERVAL_SECONDS, max(0.0, soonest))

    def _forced(self, configured: str, kid: str) -> jwt.PyJWK:
        """The key a `kid` the snapshot does not hold selects after ONE forced refresh: refused 401 only when a read of
        the issuer that STARTED after this request asked read a key set without it, else the provider's 503 saying when
        to come back.

        A forced refresh is asked at most once per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` per issuer: the `kid` is the
        caller's unverified text, so a flood of novel ones costs the issuer one read per interval. While that refresh is
        in flight, every request whose `kid` the snapshot does not hold waits for ITS outcome, at most one deadline, up to
        `FORCED_WAITERS` requests per issuer at once: so a page's parallel reads right after the person signed in again
        at a restarted issuer all find the new key, and a flood parks at most that many threads for at most one deadline
        per interval. A refusal says the token is bad, and the app asks the person to sign in again on it, so it is given
        only on that evidence. A read that had already started when this request asked may have read the set as it was
        before the key this token names was published, so finding the `kid` in what it read lets the token in, and not
        finding it says nothing (503). Whether a read has started for the ask a request joins is `forced_started`,
        recorded under the lock as each attempt starts: a request asking before that is covered by the read, one asking
        after is not. Past the cap, with the pace spent and no refresh in flight, or when the forced read failed or ran
        out, nothing is known of this `kid` either, and the issuer's state is the answer.
        """
        state = self._state[configured]
        with self._lock:
            if state.forced_asked == state.forced_served:
                if not state.pace.admit():
                    raise self._unavailable("The OIDC key set is not read again yet", self._come_back(state))
                state.forced_asked += 1
                self._wake.set()
            elif state.forced_waiters >= FORCED_WAITERS:
                raise self._unavailable("The OIDC key set is being read", FETCH_DEADLINE_SECONDS)
            asked = state.forced_asked
            # No attempt has started for this ask yet, so the one that answers it starts after this request asked.
            evidence = state.forced_started < asked
            state.forced_waiters += 1
        self.start()
        try:
            with self._lock:
                self._changed.wait_for(lambda: state.forced_served >= asked, timeout=FETCH_DEADLINE_SECONDS + 1)
                published, keys, failure = state.forced_published >= asked, state.keys, state.failure
        finally:
            with self._lock:
                state.forced_waiters -= 1
        if not published or keys is None:
            raise self._unavailable(failure or "The OIDC key set could not be read")
        key = next((key for key in keys.keys if key.key_id == kid), None)
        if key is not None:
            return key
        if not evidence:
            with self._lock:
                come_back = self._come_back(state)
            raise self._unavailable("The OIDC key set read began before this token was presented", come_back)
        raise UnauthenticatedError("Invalid or expired token")

    def _signing_key_for(self, configured: str, token: str) -> tuple[_Provider, jwt.PyJWK]:
        """The documents and the key the token's ``kid`` selects, with each failure charged to its one possible author:
        the header is the caller's, the key set the provider's."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        provider, keys, failure = self._snapshot(configured)
        # The documents first: an issuer this service cannot use is its own 503, whatever the token says.
        if provider is None:
            raise self._unavailable(failure)
        # A token naming no key selects none: `get_signing_keys` keeps only keys that carry a `kid`, so no
        # read could answer it. (A non-string `kid` never gets here — the header read above refuses it.)
        if not isinstance(kid, str):
            raise UnauthenticatedError("Invalid or expired token")
        if keys is None:
            raise self._unavailable(failure)
        key = next((key for key in keys.keys if key.key_id == kid), None)
        if key is None and self._is_retired(configured, kid):
            raise UnauthenticatedError("Invalid or expired token")
        return provider, key if key is not None else self._forced(configured, kid)

    def _is_retired(self, configured: str, kid: str) -> bool:
        """Whether ``kid`` is one ``configured`` published in a set this verifier read and left out of a later one, and
        not yet forgotten (`_retire`). Asked only of a `kid` the snapshot does not hold, so a key the issuer publishes
        again verifies again from the read that finds it back."""
        with self._lock:
            return kid in self._state[configured].retired

    def verify(self, token: str) -> IDToken:
        """Verify a bearer token and return its parsed claims.

        Raises ``UnauthenticatedError`` when the token is at fault and ``ProviderUnavailableError`` when the
        provider's documents are — the two refusals the governed doors map. Pinned by
        `packages/service-kit/tests/test_every_verifier_failure_is_a_401_or_a_503.py`.
        """
        provider, signing_key = self._signing_key_for(self._issuer_of(token), token)
        try:
            payload = jwt.decode(
                token,
                # The `PyJWK`, not its `.key`. PyJWT then verifies with the algorithm the key is bound to and
                # refuses a header naming any other (RFC 8725 §3.1: one key, one algorithm). Given the raw key
                # it prepares whatever family the header names, and a caller naming ES256 beside an RSA `kid`
                # gets a `TypeError` out of `prepare_key` rather than a `PyJWTError`.
                signing_key,
                algorithms=provider.algorithms,
                audience=self._audience,
                issuer=provider.spec.issuer,
                leeway=self._leeway,
                # PyJWT does not verify the OIDC ``at_hash`` binding (it has no access
                # token to bind against), so there is nothing to disable — unlike
                # python-jose, no ``verify_at_hash`` option exists or is needed here.
                options={
                    # ``sub`` is required here (not just in the model, `IDToken.sub`): some IdP-issued
                    # tokens legitimately omit it —
                    # without the require, such a token failed pydantic OUTSIDE the 401 mapping (500).
                    "require": ["exp", "iat", "aud", "sub"],
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
            # Inside the 401 mapping on purpose: a signed token whose claim SHAPES pydantic rejects
            # (e.g. a non-numeric exp) is a bad token — a 401, never an unhandled 500.
            return IDToken.model_validate(payload)
        except (jwt.PyJWTError, ValidationError) as exc:
            # Never leak the underlying JWT/crypto/validation error to the client.
            raise UnauthenticatedError("Invalid or expired token") from exc
