"""Shared observability init — raise the APP loggers to INFO so the audit/lifecycle log tier reaches OTLP.

The app factory puts an OTel ``LoggingHandler(NOTSET)`` on the ROOT logger when telemetry is on
(`workbench.otel.setup_otel`), but a logger left at Python's default WARNING rejects ``log.info()`` in
``isEnabledFor()``, so the LogRecord is never created and never reaches the exporter: the INFO lifecycle tier the
otel skill mandates at severity 9 would be lost, only WARNING/ERROR surviving.

So each service calls :func:`configure_app_logging` once at import time. It raises ONLY the app package loggers to
INFO, so that tier is exported WITHOUT every dependency's INFO chatter. The audit trail has its own switch
(`workbench.governed.audit.configure_audit`).
"""

from __future__ import annotations

import logging


#: The top-level app packages whose module loggers (``logging.getLogger(__name__)``) must emit INFO. Their
#: effective level inherits from these package loggers, so raising the package raises the whole tree.
#: The kit's own loggers (OpenFGA provisioning, the OIDC refresher, the factory). Each service names its own packages when
#: it calls :func:`configure_app_logging`.
_APP_LOGGERS = ("workbench",)


def configure_app_logging(level: int = logging.INFO, *, packages: tuple[str, ...] = ()) -> None:
    """Raise the app package loggers, and the caller's own ``packages``, to ``level`` so their records reach the OTLP handler."""
    for name in (*_APP_LOGGERS, *packages):
        logging.getLogger(name).setLevel(level)
