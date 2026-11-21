"""What a source declaration may carry: an allow-list, the one list the services and the chart both enforce.

A declaration lives in git, so it carries no credential: a store is reached on the catalog's vend (a lakehouse) or the
workload's own identity, and a credential that must be a file is a file mounted under :data:`MOUNT_ROOT`, named by
path. So a key is accepted only when it is on the list below for its place, and anything else is refused by name: a
deny-list of credential-looking words misses spellings (``account_key``, ``service_account_key``, ``header.Cookie``)
and refuses harmless ones (``aws_role_session_name``).

The storage keys are the object-store options `lance_docs/lance/guide/object_store.md` documents that name no secret:
the general ones (timeouts, retries, ``allow_http``), S3's region, endpoint, addressing and encryption choice and
``aws_provider_scheme``, Azure's account and tenant names and endpoint, and the keys whose value is a PATH to a mounted
credential file (Google's service account file, Azure's federated token file, the AWS web-identity token file). ``allow_invalid_certificates`` and the
proxy options are left out: one turns TLS off, the other routes every read through a host the declaration picks.

The namespace properties are the configuration properties the directory and REST implementations' pages document
(`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md` and `lance-rest.md`) but their ``headers.*`` and
``storage.*`` prefixes: a header is how a REST catalog is sent a credential, and storage options are declared once, in
``storage``. An implementation not listed takes no property, so a source of it cannot be declared until its keys are.

``python -m workbench.source_keys`` prints the list as JSON; the chart validates a release's ``sources`` against the
copy in ``chart/files/source-keys.json``, and ``make chart-check`` fails when the two differ.
"""

from __future__ import annotations

import json
import re
from typing import Final
from urllib.parse import urlsplit


#: Where a credential file a storage option names must be mounted.
MOUNT_ROOT: Final = "/var/run/secrets/"

STORAGE_KEYS: Final = frozenset(
    {
        # General (object_store.md § General Configuration).
        "allow_http",
        "block_size",
        "download_retry_count",
        "connect_timeout",
        "timeout",
        "user_agent",
        "client_max_retries",
        "client_retry_timeout",
        "scheduler_error_mode",
        # S3 (§ S3 Configuration, § Credential provider selection).
        "aws_region",
        "region",
        "aws_endpoint",
        "endpoint",
        "aws_virtual_hosted_style_request",
        "virtual_hosted_style_request",
        "aws_s3_express",
        "s3_express",
        "aws_server_side_encryption",
        "aws_sse_kms_key_id",
        "aws_sse_bucket_key_enabled",
        "aws_provider_scheme",
        # Web identity and public buckets: keys the installed pylance 13.0.0 accepts (each spelled in its library,
        # measured 2026-11-16) that object_store.md does not list, none a secret.
        "aws_role_arn",
        "aws_role_session_name",
        "aws_skip_signature",
        # Azure (§ Azure Blob Storage Configuration): names, never keys, tokens or signatures.
        "azure_storage_account_name",
        "account_name",
        "azure_endpoint",
        "azure_client_id",
        "client_id",
        "azure_tenant_id",
        "tenant_id",
        "azure_use_fabric_endpoint",
        "use_fabric_endpoint",
        "azure_disable_tagging",
        "disable_tagging",
    }
)

#: Storage keys whose value is the path of a mounted credential file, accepted only under :data:`MOUNT_ROOT`.
MOUNTED_FILE_KEYS: Final = frozenset(
    {
        "google_service_account",
        "service_account",
        "google_application_credentials",
        "application_credentials",
        "azure_federated_token_file",
        "federated_token_file",
        "aws_web_identity_token_file",
    }
)

#: The configuration properties of each lance-namespace implementation a source may name, with no credential among them.
NAMESPACE_PROPERTIES: Final = {
    "dir": frozenset({"root", "manifest_enabled", "dir_listing_enabled"}),
    "rest": frozenset({"uri", "delimiter"}),
}

#: A per-base storage key (object_store.md § Per-Base Configuration): ``base_<id>.<key>`` is held to ``<key>``'s rule.
_PER_BASE: Final = re.compile(r"^base_\d+\.(.+)$")


def check_storage(options: dict[str, str]) -> dict[str, str]:
    """``options`` when every key is an allowed storage option and every file it names is under :data:`MOUNT_ROOT`.

    Raises:
        ValueError: A key is not on the list, or a credential file lies outside the mount root.
    """
    for key, value in options.items():
        base = _PER_BASE.match(key)
        name = base.group(1) if base else key
        if name in MOUNTED_FILE_KEYS:
            if not value.startswith(MOUNT_ROOT) or "/../" in f"{value}/":
                raise ValueError(f"storage option {key!r} names a credential file outside {MOUNT_ROOT}: mount it there")
        elif name not in STORAGE_KEYS:
            raise ValueError(f"storage option {key!r} is not one a source may declare (no credential is declared; see workbench.source_keys)")
    return options


def check_properties(impl: str, properties: dict[str, str]) -> dict[str, str]:
    """``properties`` when the implementation is listed and every key is one of its non-secret properties.

    Raises:
        ValueError: The implementation takes no declared property, or a key is not on its list.
    """
    allowed = NAMESPACE_PROPERTIES.get(impl)
    if allowed is None:
        raise ValueError(f"implementation {impl!r} has no declared properties a source may name: one of {sorted(NAMESPACE_PROPERTIES)}")
    for key, value in properties.items():
        if key not in allowed:
            raise ValueError(f"property {key!r} is not one an {impl!r} source may declare (allowed: {sorted(allowed)})")
        if key in ("uri", "root"):
            check_location(value)
    return properties


def check_location(location: str) -> str:
    """``location`` when it carries no userinfo, query or fragment: a credential travels in none of them.

    Raises:
        ValueError: The location carries one.
    """
    parts = urlsplit(location)
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError(f"location {location!r} carries a user or password: a source declares no credential")
    if parts.query or parts.fragment:
        raise ValueError(f"location {location!r} carries a query or fragment, as a pre-signed URL does: a source declares no credential")
    return location


def listing() -> dict[str, object]:
    """The allow-list as the chart reads it."""
    return {
        "mountRoot": MOUNT_ROOT,
        "storage": sorted(STORAGE_KEYS),
        "mountedFiles": sorted(MOUNTED_FILE_KEYS),
        "namespaceProperties": {impl: sorted(keys) for impl, keys in sorted(NAMESPACE_PROPERTIES.items())},
    }


if __name__ == "__main__":  # pragma: no cover - `make chart-check` compares this to the chart's copy
    print(json.dumps(listing(), indent=2, sort_keys=True))
