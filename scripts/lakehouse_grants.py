"""The workbench's own grants on a rask lakehouse, made at the catalog's access doors by a person who may make them.

The workbench's services reach the lakehouse in two ways, and only one of them needs grants made here:

- **People** read with their own token (under `forward`, the person's token; under `exchange`, one exchanged for them),
  so what a person may read is that person's own grants, made by their project's administrator at the same doors. This
  script grants people nothing.
- **The workbench itself**, its confidential client signed in for itself (the machine `<idp id>-client~<client id>`),
  reads the one cell a signed media link names. It needs `reader` on each warehouse whose datasets the viewer signs
  media links for (`--reads`). It writes nothing in the lakehouse while people label: their work is the annotator's own,
  in its own database, until a publish, whose target namespace is the project's administrator's to grant.

The doors (`docs/rask/catalog-openapi.json`): `POST /management/v1/warehouse/{id}/access/{grant,revoke,list}`; `GET
/management/v1/warehouses`. rask keeps a
grant made at its doors across every upgrade of its release.

    uv run python scripts/lakehouse_grants.py {plan,grant,list,revoke} --values deploy/values-dev.yaml --reads acme --admin alice@example.com

The administrator signs in at the identity provider with the password grant on the workbench's public client; the
password is read from `--password-file` or asked for, never taken from the command line or the environment, and no
token is printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel


#: The relation the workbench's own identity holds on each warehouse it reads.
READ = "reader"


class Grant(BaseModel):
    """One base rung on one object of the lakehouse, for the workbench's own identity."""

    kind: str
    object_id: str
    relation: str


class Plan(BaseModel):
    """Everything this script makes, and for whom."""

    principal: str
    grants: list[Grant]


def plan_of(values: dict[str, Any], reads: list[str]) -> Plan:
    """The grants a deployment's values call for: the workbench client's principal, and `reader` on each warehouse it
    reads (`--reads`)."""
    oidc = (values.get("auth") or {}).get("oidc") or {}
    idp_id, client_id = oidc.get("idpId"), (oidc.get("client") or {}).get("id")
    if not (idp_id and client_id):
        raise SystemExit("the values name no auth.oidc.idpId or auth.oidc.client.id")
    if not reads:
        raise SystemExit("name each warehouse the viewer signs media links for with --reads")
    principal = f"{idp_id}-client~{client_id}"
    return Plan(principal=principal, grants=[Grant(kind="warehouse", object_id=read, relation=READ) for read in dict.fromkeys(reads)])


def _password(path: str | None) -> str:
    if path:
        return Path(path).read_text().strip()
    return getpass.getpass("the administrator's password: ")


def sign_in(http: httpx.Client, issuer: str, client_id: str, scopes: str, admin: str, password: str) -> str:
    """The administrator's token for the lakehouse, by the password grant on the workbench's public client."""
    discovery = http.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    discovery.raise_for_status()
    answer = http.post(
        discovery.json()["token_endpoint"],
        data={"grant_type": "password", "client_id": client_id, "username": admin, "password": password, "scope": scopes},
    )
    if answer.status_code != 200:
        raise SystemExit(f"the identity provider refused the administrator's sign-in ({answer.status_code}): {answer.json().get('error')}")
    return str(answer.json()["access_token"])


class Doors:
    """The catalog's doors, as the administrator."""

    def __init__(self, http: httpx.Client, catalog: str, token: str) -> None:
        self._http, self._base, self._headers = http, catalog.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._http.post(f"{self._base}{path}", json=body, headers=self._headers)

    def warehouses(self) -> list[str]:
        answer = self._http.get(f"{self._base}/management/v1/warehouses", headers=self._headers)
        answer.raise_for_status()
        return [str(warehouse["id"]) for warehouse in answer.json()]

    def access(self, grant: Grant, action: str, principal: str) -> httpx.Response:
        return self._post(f"/management/v1/{grant.kind}/{quote(grant.object_id, safe='')}/access/{action}", {"user": principal, "relation": grant.relation})

    def holders(self, grant: Grant) -> dict[str, list[str]]:
        answer = self._post(f"/management/v1/{grant.kind}/{quote(grant.object_id, safe='')}/access/list", {})
        answer.raise_for_status()
        return {str(row["relation"]): [str(user) for user in row["users"]] for row in answer.json()["grants"]}


def _say(plan: Plan) -> None:
    print(f"the workbench's own identity: {plan.principal}")
    for grant in plan.grants:
        print(f"  {grant.relation} on {grant.kind} {grant.object_id}")
    print("people read with their own token: each reader's grants are their own, made by their project's administrator.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("action", choices=["plan", "grant", "list", "revoke"])
    parser.add_argument("--values", required=True, help="the deployment's values file (its auth section names the identity)")
    parser.add_argument("--reads", action="append", default=[], help="a warehouse the viewer signs media links for (repeatable)")
    parser.add_argument("--catalog", default="http://localhost:8080/api/catalog", help="the catalog's public base")
    parser.add_argument("--issuer", help="the identity provider's issuer; default the values' auth.oidc.issuer")
    parser.add_argument("--client-id", help="the public client the administrator signs in with; default the values' app.oidc.clientId")
    parser.add_argument("--scopes", default="openid profile email federated:id audience:server:client_id:rask-lakehouse")
    parser.add_argument("--admin", help="the administrator who signs in (required but for plan)")
    parser.add_argument("--password-file", help="a file holding the administrator's password; asked for when absent")
    args = parser.parse_args(argv)

    values = yaml.safe_load(Path(args.values).read_text()) or {}
    plan = plan_of(values, args.reads)
    _say(plan)
    if args.action == "plan":
        return 0
    if not args.admin:
        parser.error("--admin names the administrator who makes the grants")
    issuer = args.issuer or ((values.get("auth") or {}).get("oidc") or {}).get("issuer")
    client_id = args.client_id or ((values.get("app") or {}).get("oidc") or {}).get("clientId") or "workbench-app"
    with httpx.Client(timeout=20.0) as http:
        doors = Doors(http, args.catalog, sign_in(http, str(issuer), client_id, args.scopes, args.admin, _password(args.password_file)))
        known = set(doors.warehouses())
        missing = sorted({grant.object_id.split("$", 1)[0] for grant in plan.grants} - known)
        if missing:
            print(f"the lakehouse holds no warehouse {', '.join(missing)}; it holds: {', '.join(sorted(known)) or 'none'}", file=sys.stderr)
            return 2
        if args.action == "grant":
            for grant in plan.grants:
                doors.access(grant, "grant", plan.principal).raise_for_status()
                print(f"granted {grant.relation} on {grant.kind} {grant.object_id} to {plan.principal}")
        elif args.action == "revoke":
            for grant in plan.grants:
                answer = doors.access(grant, "revoke", plan.principal)
                if answer.is_success:
                    print(f"revoked {grant.relation} on {grant.kind} {grant.object_id} from {plan.principal}")
                else:
                    print(f"{grant.kind} {grant.object_id}: the door answered {answer.status_code} to revoking {grant.relation}: {answer.text[:200]}")
        else:
            for grant in plan.grants:
                held = sorted(action for action, users in doors.holders(grant).items() if {plan.principal, f"user:{plan.principal}"} & set(users))
                print(f"{grant.kind} {grant.object_id}: {plan.principal} holds {', '.join(held) or 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
