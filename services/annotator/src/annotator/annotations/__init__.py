"""A task's work in progress, as the labeling canvas reads and saves it — the annotator's data plane.

Every module here reads or changes ONE store: the task's draft in the annotator's own Postgres
(`annotator.state.store`). Nothing here writes the lakehouse; a project's accepted work reaches it only at publish.

- :mod:`.schema`   — the CONTRACT: request/response models and the columns of the rows the canvas reads.
- :mod:`.access`   — whose work a door reads or writes: the task, its project and its unit.
- :mod:`.unit`     — a unit key, read against the dataset's identity.
- :mod:`.work`     — the draft as rows, and a save applied to it (pure).
- :mod:`.wire`     — Arrow-IPC serving (GET one unit's work, a past save with ``?version``).
- :mod:`.save`     — the per-unit Save (one revision of the draft).
- :mod:`.tags`     — batch tag writes (insert-if-absent tag rows).
- :mod:`.versions` — the unit's history of saves.
- :mod:`.router`   — composes the group under ``/api`` (the app includes this).

Import from the specific module — this package has no barrel.
"""
