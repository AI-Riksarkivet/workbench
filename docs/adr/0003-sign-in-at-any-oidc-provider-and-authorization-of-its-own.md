# 0003. Sign-in at any OIDC provider; authorization is the workbench's own (2026-11-14, 2026-11-16)

**Status:** accepted. Source: the owner, 2026-11-14, "this workbench has its own auth and authz that has nothing to do
with rask"; the owner's decisions on issue #7 the same day through the question tool (any OIDC provider, the workbench's
own public client and audience; the five project roles as the core of its own OpenFGA model; an organisation above
projects; flows scoped to projects; rask decides reads of rask's tables). Amended by the owner on 2026-11-16 through the
question tool (#9): "Sign-in: any OIDC provider", Dex included; token exchange (RFC 8693) and DPoP (RFC 9449) only where
the provider's discovery offers them, which replaced #7's "token exchange and DPoP required".

## Context

The extracted code signed in as rask's client, asked rask's decision door and carried rask's OpenFGA model. The dev
cluster's only identity provider is Dex, which offers neither token exchange nor DPoP (measured on Dex v2.46.1,
2026-11-16).

## Decision

- Sign-in is whatever OIDC provider the cluster runs. The workbench registers its own public PKCE client and its own
  audience there and names no provider.
- DPoP binds the tokens to the tab where the provider offers it; token exchange carries a person's identity to the
  lakehouse where the provider offers it. Where it does not, the person's token also carries the lakehouse's audience
  and is passed on unchanged.
- A person is named as rask names them (idp id and subject, a connector map, several issuers), so a grant in either
  store names the same person.
- Authorization is the workbench's own OpenFGA model and store: an organisation's admins and members, a project's five
  roles (owner > manager > reviewer > annotator > viewer), and the objects that inherit from a project. It never asks
  rask's decision door.

## Consequences

- Exchange and DPoP are proven against mocks only until a provider that offers them is deployed.
- rask knows nothing of the workbench's users or roles; rask still decides every read of its own tables.
