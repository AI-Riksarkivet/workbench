# 0012. Other people's changes appear as they happen (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18, through the question tool, "Yes, fully live": other people's edits
appear on an open canvas as they happen, chosen over a simpler notice to reload. Not built yet.

## Context

A cluster walk on 2026-11-17 found that a person's open task page does not change when a reviewer acts; they see it
only after navigating. On 2026-11-14 live refresh of datasets on new lakehouse commits was dropped "for now", the user
refreshing on demand (issue #9, D2b decision 3), because it read rask's lineage with a server-side token the
browser-only app must not hold.

## Decision

- What other people change in a project (a task's state, its work, a review) reaches an open canvas and the project's
  screens as it happens, without a reload.

## Consequences

- It needs a way for the annotator to tell the browser about a change; which one is a technical choice made in the
  row that builds it, held to the four facts.
- Whether "fully live" also covers dataset pages and new lakehouse commits is not recorded; the 2026-11-14 decision
  stands for them until the owner says otherwise.
