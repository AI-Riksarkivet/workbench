# 0022. A saved view is shared by its link (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session: saved views are shared ("Dont we have
postgres server for this?"), and by link ("share with link?").

## Context

A saved view (a dataset's layout, filters and search) is kept in the browser of the person who saved it. Issue #9's
target shape said layouts are "saved and shared". Sharing needs the view kept on a server and a rule for who may open
it.

## Decision

- A person can share a saved view. Sharing gives it a link.
- Anyone who opens the link and may read the view's dataset sees the view. A person who may not read the dataset is
  refused as for any other read of it; the link grants nothing.
- A view nobody shared stays its owner's.

## Consequences

- Shared views are kept by a service with a database, not in the browser.
- A shared view is not listed for other people: they have it by its link. Listing an organisation's shared views is a
  later decision.
- A view that names data its reader may not read, or data that is gone, must open with what the reader may see and say
  what is missing.
