"""Structured compliance audit trail — a dedicated audit-event stream, separate from operational logs.

Every security-relevant action (authn success/failure, authz allow/deny, credential vending, admin ops)
is emitted as one structured event on the ``workbench.audit`` logger: who (subject), what (action), the resource,
the outcome, and when (the record timestamp). The workbench's trail is its own, apart from any lakehouse's: with
telemetry on, its records leave over OTLP like every other log, on their own instrumentation scope, the logger's name
``workbench.audit``, queryable by ``audit.action`` / ``audit.outcome`` / ``audit.subject``. Routing that scope to a
table and retention of its own is the platform's gateway's job; one that does not stores it with the general logs.

The whole trail is gated by one flag (:func:`configure_audit`, from a service's ``WORKBENCH_AUDIT_ENABLED``
setting): a deployment turns audit on or off for its compliance posture by setting the dedicated logger's
level. Fields are consistent across every call site, so a record never misses who / what / outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final

from service_kit.context import current_request_id


#: The one well-known audit logger name (not a per-module logger) so audit records are a single stream.
AUDIT_LOGGER = "workbench.audit"
_log = logging.getLogger(AUDIT_LOGGER)

#: The record's format version, stamped on every record as ``audit.format``. A compliance query names the fields of
#: one format; a renamed or retyped field is a new format, so the query that read the old one can tell it is reading
#: records it was not written for instead of silently matching nothing.
AUDIT_FORMAT: Final = "1.0"


class AuditOutcome(StrEnum):
    """The closed outcome vocabulary a compliance query groups by: an authorization verdict, or what an action did."""

    ALLOW = "allow"
    DENY = "deny"
    SUCCESS = "success"
    FAILURE = "failure"


class AuditAction(StrEnum):
    """The kit's closed action vocabulary: what its shared doors record (who was let in, a verdict, a data read).

    An authorization verdict is ``AUTHZ`` and names the relation it decided in ``audit.relation``, so "every verdict on
    this object" is one filter however many relations the model grows. Each service records its own actions under a
    closed vocabulary of its own (`<service>.audit_actions`): :func:`audit` takes any ``StrEnum``, so no free string
    reaches the trail from anywhere.
    """

    AUTHN = "authn"
    AUTHZ = "authz"
    READ_DATA = "read_data"


def configure_audit(*, enabled: bool) -> None:
    """Enable or disable the audit stream at boot via the dedicated logger's level.

    Enabled → ``INFO`` (records emit through the OTLP handler on root). Disabled → a level above ``CRITICAL``
    so the ``INFO`` audit records are dropped at the logger, before any handler. Call once per service at
    startup. Without this call the logger inherits root (``WARNING``), so audit is OFF unless explicitly on.
    """
    _log.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)


#: What a read record names as its columns when the read was not narrowed to named ones.
ALL_COLUMNS: Final = "all"


def audit_read(
    *,
    subject: str,
    resource: str,
    version: int | None = None,
    columns: Sequence[str] | None = None,
    **fields: Any,
) -> None:
    """Record that a subject READ data — the question a lakehouse buyer expects answered (§ J1).

    The estate already authorizes every read and then forgot it happened, so "was this table ever read
    by that subject" had no answer even though allowing it was a deliberate decision. That is a
    zero-trust gap as much as a feature one.

    NOT A MIDDLEWARE, which is what § J1 first proposed. A middleware sees the path and the principal
    and nothing else, and `version` + `columns` are what make a read record worth keeping: only the
    route knows which version it served and which columns it returned. So the doors call this, and this
    fixes the action name and the field set — the part that makes the resulting log queryable.

    ``columns`` is always recorded: the names joined, or :data:`ALL_COLUMNS` when the read named none, because an
    absent field cannot be told apart from a door that forgot to say. It is joined rather than passed as a list
    because the audit record is a FLAT structure by design (`audit()`'s own contract), and a nested value would land
    differently in every sink.
    """
    audit(
        AuditAction.READ_DATA,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=resource,
        **({"version": version} if version is not None else {}),
        columns=",".join(columns) if columns else ALL_COLUMNS,
        **fields,
    )


def audit(
    action: StrEnum,
    outcome: AuditOutcome,
    *,
    subject: str | None = None,
    resource: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured audit event (dropped when audit is disabled — see :func:`configure_audit`).

    ``action`` is a member of a closed vocabulary (:class:`AuditAction` for the lakehouse); ``outcome`` one of
    :class:`AuditOutcome`; ``subject`` the verified principal; ``resource`` the object acted on. Every record carries
    ``audit.format`` (:data:`AUDIT_FORMAT`) and the request id it was made under (``audit.request_id``, ``-`` outside a
    request), so the records one request made are one filter. Extra ``fields`` (relation, tier, reason) ride the same
    record under an ``audit.`` prefix so a compliance query sees a flat, consistent schema.
    """
    _log.info(
        "audit",
        extra={
            "audit.format": AUDIT_FORMAT,
            "audit.action": action.value,
            "audit.outcome": outcome.value,
            "audit.request_id": current_request_id(),
            "audit.subject": subject or "",
            "audit.resource": resource or "",
            **{f"audit.{key}": value for key, value in fields.items()},
        },
    )
