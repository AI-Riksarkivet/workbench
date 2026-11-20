# Triage labels

The skills speak in terms of five canonical triage roles and two categories. This file maps them to the label strings
on `AI-Riksarkivet/workbench`.

| Role in mattpocock/skills | Label in our tracker | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | The owner needs to evaluate this issue |
| `needs-info` | `needs-info` | Waiting on the reporter for more information, or on an owner decision |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for the agent to work by `issue-tracker.md` |
| `ready-for-human` | `ready-for-human` | Needs a person: a judgement call, outside access, a live write the owner must approve |
| `wontfix` | `wontfix` | Will not be actioned |
| `bug` (category) | `bug` | Something is broken |
| `enhancement` (category) | `enhancement` | New behaviour or an improvement |

When a skill mentions a role (for example "apply the AFK-ready triage label"), use the label string from this table.

Labels the workbench uses beside the roles:

- `parked`: found, not admitted by the owner; uncounted and unscheduled.
- `duplicate`: closed in favour of the row its comment names.
- `wayfinder:map`: the pinned map issue (`issue-tracker.md`).
- `severity:high`, `severity:medium`, `severity:low`: a row's severity, where one was judged.

A rejected enhancement is recorded under `.out-of-scope/` by `/triage` (its `OUT-OF-SCOPE.md`); the directory is
created when the first one is written.

## Labels the workflow needs that the repository lacks

Read on 2026-11-20 with `gh label list --repo AI-Riksarkivet/workbench`. The five triage roles, `bug`, `enhancement`,
`parked`, `duplicate` and `wayfinder:map` exist. `/wayfinder`'s ticket types do not. For the owner's session to run:

```bash
gh label create "wayfinder:research"  --repo AI-Riksarkivet/workbench --color c5def5 --description "Wayfinder ticket: research"
gh label create "wayfinder:prototype" --repo AI-Riksarkivet/workbench --color c5def5 --description "Wayfinder ticket: prototype"
gh label create "wayfinder:grilling"  --repo AI-Riksarkivet/workbench --color c5def5 --description "Wayfinder ticket: grilling"
gh label create "wayfinder:task"      --repo AI-Riksarkivet/workbench --color c5def5 --description "Wayfinder ticket: task"
```

The labels cloned from rask on 2026-11-14 that name rask's own register mean nothing here: `blocks-prod`,
`criterion-1` to `criterion-5`, `phase-1`, `phase-1-after-launch`, `phase-2`, `client`, and `client:annotator`,
`client:compute`, `client:flows`, `client:frontend`, `client:runners`, `client:search`, `client:viewer`. Issue #1 still
carries `client:flows`, `client:annotator`, `client:viewer` and `client:search`. Deleting them is the owner's choice;
if chosen, `gh label delete "<name>" --repo AI-Riksarkivet/workbench --yes` for each.
