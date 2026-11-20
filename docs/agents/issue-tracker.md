# Issue tracker: GitHub

Issues and specs for this repository live as GitHub issues on `AI-Riksarkivet/workbench`. Use the `gh` CLI for every
operation. `gh` infers the repository from `git remote -v` when it runs inside a clone.

## The workbench's rules (these override the generic conventions below)

- **One issue is one row.** A row is one piece of work a person can check when it is done. Its title starts with its
  row id: `WB-101: …`. Row ids are three digits from `WB-101` up, given in the order rows are filed, never renumbered
  and never reused. Commits, comments and docs cite the row id; `#n` is GitHub's handle for the same row.
  - The ids already taken stay as they are: `WB-A` to `WB-E` (issues #6 to #10) and `WB-1` to `WB-11` (rows inside
    the move spec, #1). Three digits from 101 cannot collide with either.
  - How the six issues open on 2026-11-20 (#1, #6 to #10) map onto rows is proposed in
    `docs/planning/INPUT-2026-11-20.md` § (e); the owner settles it.
  - A spec that `/to-spec` writes is an issue too. Its title is `Spec: …` and it carries no row id; the rows
    `/to-tickets` cuts from it are its sub-issues.
- **No unnamed sub-steps inside an umbrella issue.** A piece of work is a row, or it waits until it is one. A comment
  on a spec or a parent records a decision or a landed row; it never carries a backlog of its own. (From 2026-11-16 to
  2026-11-20 the work ran as unnamed steps inside #9, with the backlog in its comments; this rule exists so that does
  not happen again.)
- **The order lives in one pinned map issue** (label `wayfinder:map`, title `Workbench map`): its body holds the
  rows in the order they are to be worked, the owner's rulings in force, and the decisions still open. The next row is
  the first one in the map's order that is unassigned and has no open blocker.
- **No new counted row without the owner.** A skill or an agent that finds a problem proposes the row; it does not
  open it. A finding the owner has not admitted carries the `parked` label and stays out of every count.

## What a row contains

- **The outcome, as a person sees it**, in one or two sentences.
- **Closes when**: one or more clauses a person can check, at least one of them on the deployed release (a browser
  walk for anything a person sees, the real client for a door). "The tests pass" is never the only clause.
- **The test seam**: where the RED test goes, preferring an existing seam and the highest one that shows the
  behaviour (the app over HTTP, a real Postgres, a real OpenFGA, the browser suite). `CLAUDE.md` § Engineering
  principles says which tests are kept.
- **Out of scope**: what a reader might expect the row to do and it does not.
- **Where it came from**: the walk, audit, issue comment or owner decision (with its date).
- **Blocked by**: native issue dependencies (see *Wayfinding operations*), never a line of prose alone.

## Working a row

The assignee field is the only record of who works on what. These hold for every row:

- **Claim first.** The first write on a row is `gh issue edit <n> --add-assignee @me`, before a branch, a test or a
  commit. An assigned row is its assignee's; a question about it goes on the issue. Unassign
  (`--remove-assignee @me`) when you stop, so it is free again.
- **Who works a row** (owner, 2026-11-20): the agent works rows without waiting for `/implement`, by these same
  rules. It claims the row, writes the RED test first (`/tdd`), runs the type checker and single test files as it
  goes and the full suite once, has the change reviewed by the Standards and the Spec reviewers in parallel (as
  `/code-review` does; a UI change gets a `playwright-cli` walk in the Spec review), fixes every finding in the
  row's own code (LOWs included), and lands one commit. `/grill-with-docs`, `/to-spec`, `/to-tickets` and `/triage`
  stay the owner's to start.
- **Coherent rows ship together, straight to `main`.** One person works this repository, so there is no branch and no
  pull request. Open rows that touch the same seam or the same deploy are one change: one review round, one commit on
  `main` naming each row with `Refs #n`, the gates once (`make test`, `make check`, `make frontend-check`,
  `dagger call app-end-to-end` at 0 failed and 0 flaky, `make chart-check` when the chart changed), one push, one
  build, one deploy, and a read-back that proves each row's closes-when. A real defect found while working is fixed
  in that change when it is in or next to the work; otherwise it is proposed as a row.
- **Reference a row with `Refs #n`, never a closing keyword.** `Closes`, `Fixes` or `Resolves` in a commit message
  would close the issue on push, and a row closes only after its read-back.
- **A row closes by hand, after it is deployed to the dev cluster and read back live** (`make deploy-dev`, or its
  `make dev-*` steps; `docs/DEPLOY.md`). The closing comment names the workbench release's helm revision, the image
  tag, and what was read back and how (the walk, the door, the people signed in). A row that cannot be shown on the
  cluster says why in its closes-when, and closes on what it can show.
- **One open row per defect.** A row found to describe the same defect as another is closed with the `duplicate`
  label and a comment naming the survivor, its evidence copied onto the survivor.

## A defect found on a live walk

A walk (a person or `playwright-cli` on the deployed release) that finds a defect does not fix it inside the row it
was walking unless the defect is in that row's own code. Otherwise:

1. Write it down where the walk is recorded (the row's closing comment), with what was done, what was seen and on
   which revision.
2. Propose it as a row: run `/triage` on it, which verifies it, recommends a category and a state, and writes the
   brief (`triage-labels.md`). Every triage comment starts with the triage disclaimer.
3. The owner admits it (the row is counted and placed in the map) or parks it (`parked`).

## Conventions

- **Create an issue**: `gh issue create --title "WB-1NN: ..." --body "..."`. Use a heredoc for a multi-line body.
- **Read an issue**: `gh issue view <number> --comments`, or `--json number,title,body,labels,comments,assignees`.
- **List issues**: `gh issue list --state open --json number,title,labels,assignees` with `--label` and `--state`
  filters.
- **Make an issue a sub-issue of a parent**: `gh issue create --parent <parent> ...`, or
  `gh issue edit <parent> --add-sub-issue <child>` afterwards (`gh` 2.94+). Older `gh`:
  `gh api --method POST repos/AI-Riksarkivet/workbench/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- **Close**: `gh issue close <number> --comment "..."` (the comment names the helm revision; see above).

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads
this flag.)_ Dependabot's pull requests are dependency updates, not requests: they are handled by a hygiene row, not by
triage.

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `AI-Riksarkivet/workbench`, titled by the rules above.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder` and by the map. The **map** is a single issue with **child** issues as rows.

- **Map**: the issue labelled `wayfinder:map` (title `Workbench map`), pinned, holding the order, the rulings in force
  and the open decisions.
- **Child row**: an issue linked to the map as a GitHub sub-issue. Where sub-issues are not enabled, add the child to a
  task list in the map body and put `Part of #<map>` at the top of the child body. `/wayfinder` tickets also carry
  `wayfinder:<type>` (`research`, `prototype`, `grilling`, `task`).
- **Blocking**: GitHub's native issue dependencies. Add an edge with
  `gh api --method POST repos/AI-Riksarkivet/workbench/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`,
  where `<blocker-db-id>` is the blocker's numeric database id
  (`gh api repos/AI-Riksarkivet/workbench/issues/<n> --jq .id`, not the `#number`). GitHub reports
  `issue_dependencies_summary.blocked_by` (open blockers only). Where dependencies are not available, fall back to a
  `Blocked by: #<n>, #<n>` line at the top of the child body. A row is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children, drop any with an open blocker or an assignee; the first in the
  map's order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: comment with the answer or the read-back, close the issue, then add a one-line pointer to the map's
  Decisions-so-far.
