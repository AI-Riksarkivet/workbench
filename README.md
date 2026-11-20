# workbench

Explore, search and annotate datasets, and label them together with models, as one browser app. The workbench reads any
Lance source a deployment declares: a [rask](https://github.com/AI-Riksarkivet/rask) lakehouse through its published
doors and the credentials its catalog vends, any lance-namespace implementation, or a bare Lance table. It runs with or
without rask, and is never part of it.

- **explorer**: the viewer, search, and a dataset's views in the app
- **annotator**: projects, tasks, the labeling canvas, review and publish, with assist asking a model endpoint the deployment names (`docs/model-protocol.md`)
- **flows**: model pipelines; parked, with no screen and no deployment (`docs/adr/0011`)

The work is tracked as GitHub issues, in the order of the pinned "Workbench map" issue; the decisions are in
`docs/adr/`. Development: `make install`, then `make local` to run it on demo data, `make check` and `make test`. Images
are built with Dagger: `dagger call image --name=<stem>`.

Licensed under the Apache License 2.0 (`LICENSE`). Third-party code keeps its own licence and notice.
