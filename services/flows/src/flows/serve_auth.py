"""The flows service's own credential to Ray Serve: a projected ServiceAccount token with audience ``serve``.

Serve must not admit a caller for where it sits on the network, so every call carries the flows service's own
short-lived identity. A Kubernetes projected ServiceAccount token (the TokenRequest API's bound token: a JWT, RFC 7519,
naming its audience and expiring within the hour) is that identity with no secret to hold: the kubelet writes it to a
mounted file and replaces it before it expires, so it is read on every call and sent as a bearer (RFC 6750 § 2.1).
Serve is a workload of the compute repository, and verifying the token (its issuer, the ``serve`` audience, the
``flows`` ServiceAccount as subject, its expiry) is what compute must enforce.
"""

from __future__ import annotations

from pathlib import Path


class ServeCredentialError(RuntimeError):
    """This service has no credential to present to Serve: none is configured, or its file cannot be read."""


def serve_authorization(token_file: str | None) -> dict[str, str]:
    """The ``Authorization`` header for a call to Serve, from the token in ``token_file`` read now.

    Raises:
        ServeCredentialError: no token file is configured, or it is unreadable or empty.
    """
    if not token_file:
        raise ServeCredentialError("this flows service names no Serve credential: set WORKBENCH_FLOWS_SERVE_TOKEN_FILE")
    try:
        token = Path(token_file).read_text().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ServeCredentialError(f"the Serve credential at {token_file} could not be read") from exc
    if not token:
        raise ServeCredentialError(f"the Serve credential at {token_file} is empty")
    return {"Authorization": f"Bearer {token}"}
