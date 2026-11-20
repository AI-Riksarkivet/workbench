# Architecture decision records

One numbered file per decision, so code, rows and docs cite a permanent record. This is the workbench's only decision
log; the history the comment rule in `CLAUDE.md` keeps out of code comments belongs here or in a commit message.

## Numbering and format

- A record is one file, `NNNN-<slug>.md`, numbered in the order it was recorded, from `0001`. A new decision takes the
  next free number; a number is never reused, and a record is never renumbered.
- The title is `# NNNN. <the decision, as a sentence> (<date decided>)`.
- A `**Status:**` line sits under the title, with the decision's source: who decided, when, and through what (the
  question tool, an issue comment, a commit), quoting the owner's words where they were recorded. Where a reason or a
  word was not recorded, the record says "not recorded" rather than supplying one.
- Then three short sections: **Context** (what the decision answered), **Decision**, **Consequences** (what follows,
  including what it rules out). A **Considered options** section is added only when the rejected options are worth
  remembering, each judged against the four facts (`CLAUDE.md`).
- Status values: **accepted** (it describes how the workbench works or will work), **amended** (a later record changed
  part of it; both name each other), **superseded by NNNN** (a later record replaced it), **retired** (what it decided
  about is gone). A superseded or retired record is kept as written.
- An ADR the owner decided is reopened only by the owner. An agent that finds one wrong says "reopen ADR NNNN" with the
  evidence, in a question to the owner.

Records 0001 to 0017 were written on 2026-11-20 from decisions already made between 2026-11-06 and 2026-11-18, recorded
in issues #1, #7, #9 and #10, their comments, and the commit messages named in each.

## Index

| ADR | Decision | Decided | Status |
| --- | --- | --- | --- |
| [0001](0001-the-workbench-is-a-client-of-rask-or-runs-without-it.md) | The workbench is a client of rask through its published contract, or runs without rask on any Lance source | 2026-11-06, 2026-11-16 | accepted |
| [0002](0002-one-browser-only-app.md) | One browser-only app, no micro-frontends | 2026-11-14 | accepted |
| [0003](0003-sign-in-at-any-oidc-provider-and-authorization-of-its-own.md) | Sign-in at any OIDC provider; authorization is the workbench's own | 2026-11-14, 2026-11-16 | accepted |
| [0004](0004-work-in-progress-lives-in-the-annotators-postgres-until-publish.md) | Work in progress lives in the annotator's Postgres until publish | 2026-11-17 | accepted |
| [0005](0005-data-outside-rask-is-labelable-only-when-marked-in-git.md) | Data outside rask is labelable only when its source is marked annotatable in git | 2026-11-17 | accepted |
| [0006](0006-classes-changed-after-sending-reach-unstarted-items-only.md) | Classes changed after sending reach only items nobody has started | 2026-11-17 | accepted |
| [0007](0007-every-canvas-renders-with-webgpu-only.md) | Every canvas renders with WebGPU only | 2026-11-14 | accepted |
| [0008](0008-the-dock-is-horizon-layout.md) | The dock is horizon-layout, in the app's own code | 2026-11-14 | accepted |
| [0009](0009-the-workbench-runs-no-dapr.md) | The workbench runs no Dapr | 2026-11-16 | accepted |
| [0010](0010-a-signed-media-link-is-the-permission.md) | A signed media link is the permission | 2026-11-14 | accepted |
| [0011](0011-flows-is-parked.md) | Flows is parked | 2026-11-18 | accepted |
| [0012](0012-updates-are-fully-live.md) | Other people's changes appear as they happen | 2026-11-18 | accepted |
| [0013](0013-publish-has-three-targets.md) | Publish has three targets: a rask table, a Lance table on S3, export files | 2026-11-18 | accepted |
| [0014](0014-a-descriptor-comes-from-a-pipeline-stamp-or-a-person.md) | A table's descriptor comes from a pipeline stamp or from a person in the app | 2026-11-18 | accepted |
| [0015](0015-the-design-system-lives-in-its-own-repository.md) | The design system lives in AI-Riksarkivet/ui, and Svelte Bits is never copied into it | 2026-11-18 | accepted |
| [0016](0016-the-app-takes-its-design-system-by-a-pinned-commit.md) | The app takes its design system by a pinned commit | 2026-11-18 | accepted |
| [0017](0017-colours-are-the-persons-theme.md) | Colours are the person's theme | 2026-11-18 | accepted |
| [0018](0018-supported-browsers-are-chromium-with-a-gpu.md) | The supported browsers are current Chrome and Edge on a machine with a GPU | 2026-11-20 | accepted |
| [0019](0019-an-unconfirmed-proposal-cannot-be-submitted.md) | An item with an unconfirmed proposal cannot be submitted | 2026-11-20 | accepted |
| [0020](0020-a-reload-keeps-the-session-through-the-issuers-own.md) | A reload keeps the session through the issuer's own session, with no token stored | 2026-11-20 | accepted |
| [0021](0021-the-old-frontends-are-deleted-while-flows-is-parked.md) | The old frontends are deleted while flows is parked | 2026-11-20 | accepted; amends 0011 |
| [0022](0022-a-saved-view-is-shared-by-its-link.md) | A saved view is shared by its link | 2026-11-20 | accepted |
| [0023](0023-the-workbench-calls-model-endpoints-and-deploys-none.md) | The workbench calls model endpoints and deploys no model | 2026-11-20 | accepted |
| [0024](0024-the-code-is-licensed-apache-2-0.md) | The code is licensed Apache-2.0 | 2026-11-20 | accepted |
| [0025](0025-live-covers-a-projects-screens-and-the-canvas.md) | Live updates cover a project's screens and the canvas; dataset pages wait | 2026-11-20 | accepted; scopes 0012 |
| [0026](0026-a-judge-flags-labels-for-a-reviewer-and-decides-nothing.md) | A judge flags labels for a reviewer and decides nothing | 2026-11-20 | accepted |
| [0027](0027-sampling-orders-a-queue-and-selects-items-by-a-projects-strategy.md) | Sampling orders a queue and selects items, by a project's strategy | 2026-11-20 | accepted |
| [0028](0028-the-learning-loop-trains-outside-the-workbench.md) | The learning loop trains outside the workbench | 2026-11-20 | accepted |
| [0029](0029-a-first-model-slice-comes-before-the-loop-on-the-lakehouse.md) | A first model slice comes before the loop on the lakehouse | 2026-11-20 | accepted |
| [0030](0030-the-workbench-states-its-own-model-protocol.md) | The workbench states its own model protocol | 2026-11-20 | accepted; its adapter part replaced by 0032 |
| [0031](0031-a-project-may-accept-confident-proposals-with-a-sample-checked-by-a-person.md) | A project may accept confident proposals, with a sample checked by a person | 2026-11-20 | accepted; amends 0019 |
| [0032](0032-a-model-backend-is-a-ray-serve-deployment-that-speaks-the-protocol.md) | A model backend is a Ray Serve deployment that speaks the protocol | 2026-11-20 | accepted; replaces the adapter part of 0030 |
