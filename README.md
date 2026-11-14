# workbench

Explore, search and annotate the datasets in a [rask](https://github.com/AI-Riksarkivet/rask) lakehouse, and run model
pipelines over them. The workbench is a client of rask: it reaches a lakehouse only through rask's published API and
the credentials its catalog vends.

- **explorer**: the viewer, search and the explorer UI
- **annotator**: the annotator, its assist runner and its UI
- **flows**: model pipelines and the studio UI

Extracted with its history from rask at `64ef118a9` (2026-11-14); see issue #1 for the plan. Development: `make install`,
then `make check` and `make test`. Images are built with Dagger: `dagger call image --name=<stem>`.
