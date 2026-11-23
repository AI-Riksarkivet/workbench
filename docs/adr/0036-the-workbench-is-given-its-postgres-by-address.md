# 0036. The workbench is given its PostgreSQL by address and runs no database operator (2026-11-23)

**Status:** accepted. Source: the owner, 2026-11-23, in the session that worked #43, after the development estate lost
the CloudNativePG operator: "why does it need an operator that the platform sets up?", and "Yes please" to the proposal
that the chart take a PostgreSQL address as it takes OpenFGA. The fixture's shape (one server, a database each, a
certificate made at install) is the agent's that built it; the owner has not ruled on it.

## Context

The chart rendered the annotator's state as a CloudNativePG `Cluster`, and the development OpenFGA fixture rendered its
database the same way. Why CloudNativePG was chosen was not recorded. Every cluster the workbench was installed on
therefore had to run that operator, which the workbench never installed: on the development estate it used the one
rask's fixtures installed. On 2026-11-23 rask stopped running any PostgreSQL of its own (rask#640: it takes an address
from the platform) and the operator was removed from the estate, which deleted both of the workbench's Clusters.

## Decision

The annotator's PostgreSQL is a platform service the release is given by reference, like the identity provider and
OpenFGA: `state` names the host, port, database and user, the Secret holding the role's password, and the Secret
holding the certificate authority the server's certificate is verified against. The chart renders no database, asks
for no operator, and always connects with `verify-full`. The database and its role exist before the install; the
annotator applies its own schema at start, as before.

On the development estate, `deploy/fixtures/postgres` stands in for the platform's: one stock PostgreSQL with a
database and a role each for the annotator's state and the development OpenFGA's tuples, every password generated in
the cluster, and a certificate signed at install by an authority whose key is then discarded.

## Consequences

- Cloud native: a platform service is named, never bundled. A platform runs PostgreSQL by whatever means it has
  (an operator, a managed service) and the workbench does not know which.
- GitOps: the release is the same objects on every cluster; nothing in it depends on an operator's API being served.
  The development fixture is outside that claim: its certificate is made by Helm at install and kept by reading the
  cluster, so a render with no cluster makes a new one each time. A platform's certificate is the platform's.
- The TLS mode is not a value: the chart always sets `verify-full`, which the annotator requires off the loopback.
- Zero trust: the password stays a mounted file read on each new connection, the server is verified on every
  connection, and each role may connect only to its own database.
- Backups, failover and upgrades of the database are the platform's, where the `Cluster` used to offer them through
  the operator. The development fixture has one instance and no backup.
- The chart no longer says who may reach the database: that policy belongs to whoever runs it. The fixture admits only
  its namespace's pods.
- Flows, when it gets a chart workload, is given a database the same way.
