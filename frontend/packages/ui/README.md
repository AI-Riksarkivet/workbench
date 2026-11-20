# @workbench/ui (frozen)

The workbench's old copy of its design system, kept only because the old apps (`frontend/annotator`,
`frontend/explorer`, `frontend/studio`) still import it. It is frozen: nothing new is added to it and it is not
changed, and it is deleted with the last of those apps (issue #9, steps D3c and D4).

The design system is [`AI-Riksarkivet/ui`](https://github.com/AI-Riksarkivet/ui) (`@riksarkivet/ui`), which the app
(`frontend/app`) installs pinned to a commit. Components, tokens, themes and their Storybook are changed there; the
app's lint refuses an import of `@workbench/ui`.
