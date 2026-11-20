# Domain docs

How the engineering skills consume this repository's domain documentation.

The workbench is **single-context**: one glossary, `GLOSSARY.md` at the repository root, and numbered ADRs in
`docs/adr/` (index and format in `docs/adr/README.md`). `docs/adr/` is the only decision log; it also holds the history
the comment rule in `CLAUDE.md` keeps out of code comments.

**The glossary is `GLOSSARY.md`,** the name the installed `mattpocock-skills` (1.3.1) read and write. There is one
glossary and no `GLOSSARY-MAP.md`.

**Run the planning skills from this repository.** `/grill-with-docs`, `/domain-modeling`, `/to-spec`, `/to-tickets`
and `/triage` write `GLOSSARY.md`, ADRs and issues relative to the session's working directory and its `git remote`. A
session opened in rask's checkout writes them into rask. Start them only in a session whose working directory is a
checkout of `AI-Riksarkivet/workbench`.

## Before exploring, read these

- **`GLOSSARY.md`** at the repository root.
- **`docs/adr/`**: the ADRs that touch the area you are about to work in. Each names its source (the owner's words and
  date where recorded).
- **`docs/rask/`**: rask's published contract, vendored (`docs/rask/PROVENANCE.md` names the rask commit). A claim about
  what rask does is settled there, never from memory; compare the pinned commit with rask's `origin/main` before work
  that touches rask.

If one of these is missing, proceed silently; `/domain-modeling` creates terms and ADRs when they are resolved.

## Judge every output by the four facts

Every design, option, spec, row, refactor proposal and review is held to the four facts in `CLAUDE.md` § Engineering
principles, GitOps first: **GitOps** (the release is what git declares, reconciled with no imperative step), **cloud
native**, **Lance-namespace/table-idiomatic** and **zero trust**. Name the fact each option meets or breaks; an option
that breaks one is offered only with that said.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the
term as `GLOSSARY.md` defines it, and do not drift to a word it lists under _Avoid_. Where the code still uses an avoided
word, that is a rename to propose, not a reason to use the word.

If the concept you need is not in the glossary, either you are inventing language the project does not use
(reconsider) or there is a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an ADR, say so explicitly rather than silently overriding it:

> _Contradicts ADR-0004 (work in progress in the annotator's Postgres), but worth reopening because…_

An ADR whose decision the owner made is reopened only by the owner.
