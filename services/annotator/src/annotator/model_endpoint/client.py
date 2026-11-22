"""The caller's side of the model protocol: the annotator asking the model endpoint its deployment names.

One endpoint per deployment (`ANNOTATOR_MODEL_ENDPOINT_URL`), called as the annotator itself: every call presents the
token in `ANNOTATOR_MODEL_ENDPOINT_TOKEN_FILE`, read when the call is made, so a rotated token is the next call's. A
deployment that names no endpoint has none, and says so (`ModelEndpointUnavailable`).

What an endpoint does wrong is never an empty answer. Each failure is an error of its own, with the sentence a person
reads: no answer in time (504), an answer that failed or is outside the protocol (502), busy (503 with the endpoint's
``Retry-After``), an ask the model refused (422, in the endpoint's words) and a model the endpoint does not serve (404).
"""

from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from annotator.model_endpoint.protocol import (
    ASSESSMENTS_PATH,
    MODELS_PATH,
    PROPOSALS_PATH,
    AssessmentRequest,
    AssessmentResponse,
    ModelListing,
    ProposalsRequest,
    ProposalsResponse,
)
from workbench.exceptions import DomainError, NotFoundError, ServiceUnavailableError


logger = logging.getLogger(__name__)

#: How long the model listing may take: it reads no item and runs no model.
LISTING_TIMEOUT_SECONDS: Final = 5.0
#: The longest detail of an endpoint's problem body a person is shown.
MAX_DETAIL: Final = 500
#: The largest answer read from an endpoint, an error's body included.
MAX_ANSWER_BYTES: Final = 32 * 1024 * 1024


NO_ENDPOINT: Final = "assist is unavailable: this deployment names no model endpoint"


class ModelEndpointUnavailable(ServiceUnavailableError):
    """This deployment names no model endpoint: ``detail`` says what is unavailable for it."""

    problem_type = "about:blank#no-model-endpoint"

    def __init__(self, detail: str = NO_ENDPOINT) -> None:
        super().__init__(detail)


class ModelEndpointBusy(ServiceUnavailableError):
    """The endpoint answered 429 or 503: it cannot take the ask now."""

    problem_type = "about:blank#model-endpoint-busy"


class ModelEndpointTimeout(DomainError):
    """The endpoint did not answer in time."""

    status_code = HTTPStatus.GATEWAY_TIMEOUT
    title = "Gateway Timeout"
    problem_type = "about:blank#model-endpoint-timeout"


class ModelEndpointFailed(DomainError):
    """The endpoint could not be reached, failed, refused the annotator's identity, or answered outside the protocol."""

    status_code = HTTPStatus.BAD_GATEWAY
    title = "Bad Gateway"
    problem_type = "about:blank#model-endpoint-failed"


class ModelRefusedAsk(DomainError):
    """The model refused this ask (a prompt it does not take, an item it cannot read)."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unprocessable Content"
    problem_type = "about:blank#model-refused-ask"


TOO_LARGE: Final = "the model endpoint answered more than the workbench reads"


class _Answer(BaseModel):
    """What was read of an endpoint's response: its status, its `Retry-After`, and its body, never past the cap."""

    status: int
    retry_after: str
    body: bytes

    @property
    def said(self) -> str:
        """What the endpoint said went wrong: its problem body's ``detail``, else its title, else nothing."""
        try:
            body = json.loads(self.body)
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        return str(body.get("detail") or body.get("title") or "")[:MAX_DETAIL]


async def _read(response: httpx.Response) -> bytes:
    """The response's body, refused once it is past `MAX_ANSWER_BYTES`: by its declared length before a byte is read,
    else as it arrives, so an endpoint cannot make the annotator hold more than the cap."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_ANSWER_BYTES:
        raise ModelEndpointFailed(TOO_LARGE)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_ANSWER_BYTES:
            raise ModelEndpointFailed(TOO_LARGE)
    return bytes(body)


class ModelEndpoint(BaseModel):
    """The deployment's model endpoint, or the lack of one."""

    url: str | None
    token_file: Path | None
    timeout_seconds: float

    @property
    def configured(self) -> bool:
        """Whether this deployment names an endpoint."""
        return self.url is not None

    def _authorization(self) -> dict[str, str]:
        """The header naming the annotator to the endpoint, from its token file as it is now. Blocking (a file read)."""
        if self.token_file is None:
            raise ModelEndpointFailed("the model endpoint cannot be called: the annotator has no token to present to it")
        try:
            token = self.token_file.read_text().strip()
        except OSError as exc:
            raise ModelEndpointFailed("the model endpoint cannot be called: the annotator's token for it could not be read") from exc
        if not token:
            raise ModelEndpointFailed("the model endpoint cannot be called: the annotator's token for it is empty")
        return {"Authorization": f"Bearer {token}"}

    async def _exchange(self, method: str, url: str, headers: dict[str, str], content: bytes | None, timeout: float) -> _Answer:
        """One request and its whole answer, within ``timeout`` seconds in all: httpx's own timeout bounds each read, so
        an endpoint that sends a byte now and then is cut by the deadline around the exchange."""
        try:
            async with asyncio.timeout(timeout):
                # No redirect is followed: the token is presented to the address the deployment named and to no other.
                async with (
                    httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http,
                    http.stream(method, url, headers=headers, content=content) as response,
                ):
                    return _Answer(status=response.status_code, retry_after=response.headers.get("retry-after", ""), body=await _read(response))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ModelEndpointTimeout(f"the model endpoint did not answer within {timeout:g} s") from exc
        except httpx.HTTPError as exc:
            logger.warning("the model endpoint could not be reached: %s", type(exc).__name__)
            raise ModelEndpointFailed("the model endpoint could not be reached") from exc

    async def _call(self, method: str, path: str, *, timeout: float, body: BaseModel | None = None) -> bytes:
        if self.url is None:
            raise ModelEndpointUnavailable
        headers = await run_in_threadpool(self._authorization)
        content = body.model_dump_json().encode() if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        answer = await self._exchange(method, self.url.rstrip("/") + path, headers, content, timeout)
        if HTTPStatus.OK <= answer.status < HTTPStatus.MULTIPLE_CHOICES:
            return answer.body
        suffix = f": {answer.said}" if answer.said else ""
        match answer.status:
            case HTTPStatus.NOT_FOUND:
                raise NotFoundError(f"the model endpoint serves no such model{suffix}")
            case HTTPStatus.UNPROCESSABLE_ENTITY | HTTPStatus.REQUEST_ENTITY_TOO_LARGE | HTTPStatus.BAD_REQUEST:
                raise ModelRefusedAsk(f"the model refused the ask{suffix}")
            case HTTPStatus.TOO_MANY_REQUESTS | HTTPStatus.SERVICE_UNAVAILABLE:
                retry = {"Retry-After": answer.retry_after} if answer.retry_after.isdigit() else None
                raise ModelEndpointBusy(f"the model endpoint is busy or unavailable for now{suffix}", headers=retry)
            case HTTPStatus.UNAUTHORIZED | HTTPStatus.FORBIDDEN:
                logger.warning("the model endpoint refused the annotator's identity with %d", answer.status)
                raise ModelEndpointFailed(f"the model endpoint refused the workbench's own identity ({answer.status})")
            case _:
                raise ModelEndpointFailed(f"the model endpoint failed ({answer.status}){suffix}")

    async def models(self) -> ModelListing:
        """The models the endpoint serves.

        Raises:
            ModelEndpointUnavailable: The deployment names no endpoint.
            ModelEndpointTimeout: No answer in time.
            ModelEndpointBusy: The endpoint cannot answer now.
            ModelEndpointFailed: Unreachable, failed, or an answer outside the protocol.
        """
        answer = await self._call("GET", MODELS_PATH, timeout=min(LISTING_TIMEOUT_SECONDS, self.timeout_seconds))
        try:
            return ModelListing.model_validate_json(answer)
        except ValidationError as exc:
            raise ModelEndpointFailed("the model endpoint's list of models is outside the protocol") from exc

    async def proposals(self, ask: ProposalsRequest) -> ProposalsResponse:
        """One model's proposals on one item.

        Raises:
            ModelEndpointUnavailable: The deployment names no endpoint.
            ModelEndpointTimeout: No answer in time.
            ModelEndpointBusy: The endpoint cannot take the ask now.
            ModelRefusedAsk: The model refused the ask.
            NotFoundError: The endpoint serves no such model.
            ModelEndpointFailed: Unreachable, failed, or an answer outside the protocol.
        """
        read = await self._call("POST", PROPOSALS_PATH, timeout=self.timeout_seconds, body=ask)
        try:
            answer = ProposalsResponse.model_validate_json(read)
        except ValidationError as exc:
            raise ModelEndpointFailed("the model endpoint's answer is outside the protocol") from exc
        if answer.model != ask.model:
            raise ModelEndpointFailed(f"the model endpoint answered as {answer.model!r}, and {ask.model!r} was asked")
        return answer

    async def assessment(self, ask: AssessmentRequest) -> AssessmentResponse:
        """What one model disagrees with among an item's labels. Raises as `proposals` does; an answer naming a label
        the ask did not carry is outside the protocol."""
        read = await self._call("POST", ASSESSMENTS_PATH, timeout=self.timeout_seconds, body=ask)
        try:
            answer = AssessmentResponse.model_validate_json(read)
        except ValidationError as exc:
            raise ModelEndpointFailed("the model endpoint's assessment is outside the protocol") from exc
        if answer.model != ask.model:
            raise ModelEndpointFailed(f"the model endpoint answered as {answer.model!r}, and {ask.model!r} was asked")
        asked = {label.id for label in ask.labels}
        if any(d.label_id is not None and d.label_id not in asked for d in answer.disagreements):
            raise ModelEndpointFailed("the model endpoint's assessment names a label it was not asked about")
        return answer


__all__ = [
    "NO_ENDPOINT",
    "ModelEndpoint",
    "ModelEndpointBusy",
    "ModelEndpointFailed",
    "ModelEndpointTimeout",
    "ModelEndpointUnavailable",
    "ModelRefusedAsk",
]
