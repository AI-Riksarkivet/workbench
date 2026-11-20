# Workbench

The workbench explores, searches and labels Lance datasets, as one browser app over a few services. It is a client of a
rask lakehouse, or runs with no lakehouse on any Lance-namespace catalog or bare Lance tables.

Each term says what it is in one or two sentences, then where it lives (_Lives in_), then the words not to use for it
(_Avoid_). Where the code still uses an avoided word, the UI's word is the one kept and the other is a rename to make.

## Language

### Data

**Source**:
A place the workbench reads Lance data from, declared in git with a name and a kind: `rask` (a lakehouse, through its
catalog), `namespace` (any Lance-namespace catalog) or `tables` (a bare `.lance` table or a directory of them).
_Lives in_: `client_kit.sources`; the chart's `sources` value; `scripts/local-sources.json`.
_Avoid_: store, backend, catalog (for the source itself)

**Annotatable**:
Whether a source's datasets may be sent to a project and labelled. A lakehouse's always are; any other source's only
when git marks it `annotatable: true`.
_Lives in_: `client_kit.sources.annotatable`; ADR 0005.
_Avoid_: labelable, writable

**Reader (of a source)**:
A person or organisation git declares may read a source that the workbench authorizes itself (any source that is not a
lakehouse read as the person). A source that names no reader is read by nobody.
_Lives in_: a source's `readers`; `reader` on `source:<name>` in the workbench's OpenFGA.
_Avoid_: viewer (that is a project role)

**Dataset**:
A namespace of a source that holds tables, at any depth, named `<source>~<namespace>` (`lake~acme$corpus`, `scans~`).
It is what the Datasets page lists and what a dataset page opens.
_Lives in_: `client_kit.lancekit.registry`; `/datasets/[id]`.
_Avoid_: corpus (the annotator's code name for it), collection, table

**Descriptor**:
The declaration that tells the workbench a dataset's roles: which fields are a row's identity, where its media bytes
are, what to display, how search binds to its columns. It is read from the `lance_media.descriptor` stamp a pipeline
writes on a table.
_Lives in_: `client_kit.lancekit.descriptor`; ADR 0014.
_Avoid_: schema, metadata, config

**Stamped dataset**:
A dataset whose descriptor is present and valid. Only a stamped dataset of an annotatable source can be sent to a
project today.
_Avoid_: media dataset, rich dataset

**Plain table**:
A dataset with no descriptor (or an invalid one), shown from its schema: rows, sorting where Lance orders a column, text
search only where a full-text index is, similar rows only where a vector index is.
_Lives in_: `viewer.services.plain_rows`; the Rows and Row panels; `#lib/plain`.
_Avoid_: raw table, unstamped dataset

**Set-up**:
A person's choice of a plain table's columns for its Atlas, Graph or Tree, confirmed before anything is drawn and kept
in a saved view. It writes nothing to the source.
_Lives in_: `viewer.services.derived_views`; `#lib/plain/SetupForm.svelte`.
_Avoid_: config, mapping

### Exploring

**Dataset page**:
One dock per dataset, its only navigation; every view of the dataset is a panel in it, and the address carries the view.
_Lives in_: `/datasets/[id]`; `#lib/dock`.
_Avoid_: workbench page, explorer

**Dock**:
The arrangement of panels on a dataset page, split and tabbed, kept per dataset in this browser. It is horizon-layout.
_Lives in_: `#lib/dock`; ADR 0008.
_Avoid_: workspace, layout manager, dockview

**Panel**:
One view inside the dock: Results, Player, Atlas, Graph, Tree, and on a plain table Rows and Row. A panel the dataset
cannot feed is offered disabled with its reason.
_Lives in_: `#lib/dock/panels`; `panelGates` in `#lib/explore/pane-capabilities.ts`.
_Avoid_: pane, widget, tab (a tab is a panel's handle)

**Saved view**:
A named arrangement of a dataset page: the dock's panels, a plain table's set-ups, and the search or node its rows
listed. A dataset may have several and one default. Kept in this browser.
_Lives in_: the Views rail, `#lib/dock/views.svelte.ts`.
_Avoid_: layout (the older name)

**Saved search**:
A search kept under a name beside the search bar, per dataset, in this browser. It is not a saved view.
_Lives in_: `#lib/explore/components/saved-views.svelte`, `#lib/explore/saved-views.svelte.ts`.
_Avoid_: saved view (the code's file names use it for this; a rename to make)

**Signed media link**:
A short-lived link the viewer signs for one media cell after checking the person's own read; the link is the permission
for `<img>` and `<video>`, which send no token.
_Lives in_: `viewer.api.v1.endpoints.signed_media`; ADR 0010.
_Avoid_: presigned URL (that is a store's own link, used only for an External blob)

### Projects

**Organisation**:
The group above projects. Its admins administer it and its members may create and list projects.
_Lives in_: `org:<name>` in the workbench's OpenFGA; declared in the chart's `auth.fga.orgs`.
_Avoid_: tenant (the annotator's field name), org (in prose), workspace

**Project**:
A piece of labeling work: the items sent to it, its ontology, its rules (review, consensus, lease), its people and
their roles. It is published as a whole.
_Lives in_: `annotator.projects.models.AnnotationProject`; `/projects/[project]`.
_Avoid_: job, campaign

**Project state**:
Where a project stands: draft, labeling, frozen, publishing, published, publish failed, archived. Items are sent only
in draft or labeling; a publish starts only from frozen (or after a failed one).
_Lives in_: `annotator.projects.machines.PROJECT_EDGES`.

**Role**:
What a person may do on a project, highest first: owner > manager > reviewer > annotator > viewer. Each role holds every
right of the roles below it; an organisation's members are viewers of its projects.
_Lives in_: `service_kit/governed/auth/model.fga`.
_Avoid_: permission (a permission is one right a role grants, such as `can_review`)

**Ontology**:
A project's one definition of what is labelled: its classes, the tools each class is drawn with, their attributes, and
the relations between shapes. A task keeps the ontology it was sent with; a class changed later reaches only items
nobody has started.
_Lives in_: `annotator.projects.ontology.LabelOntology`; ADR 0006.
_Avoid_: label schema, template, taxonomy

**Class**:
One entry of the ontology: a name a shape is labelled with, its colour, and the tools it may be drawn with. Its keys on
the canvas and in the Label grid are the digits 1 to 9 in the ontology's order.
_Avoid_: label (a label is a class given to a shape), category

### Items and work

**Item**:
One thing sent to a project to be labelled: a page, a recording, a film or a text row of a dataset, captured with its
dataset's table and version at send.
_Lives in_: a task's `source` (`ItemSource`).
_Avoid_: chunk, sample, document (a document is the media an item may belong to)

**Unit**:
The row an item names, written as a key path with one segment per identity field of the descriptor. A task's work is
its item's first unit.
_Lives in_: `annotator.annotations.unit`; `access.work_unit`.
_Avoid_: key (the key is how a unit is written), frame

**Task**:
One item's place in a project, holding its state, who holds it, its review history and its draft. An item sent to a
consensus project becomes several tasks, one per replica.
_Lives in_: `annotator.projects.models.Task`.
_Avoid_: job, assignment

**Task state**:
Where a task stands: unassigned, claimed, in review, changes requested, accepted, skipped.
_Lives in_: `annotator.projects.machines.TASK_EDGES`.

**Claim**:
A person taking an unassigned (or sent-back) task to label it. Only the holder of a claim edits the task's work.
_Lives in_: the `claim` event; a manager's `assign` makes the same claim for someone else.
_Avoid_: lock, checkout

**Lease**:
How long a claim lasts unless renewed, read against the database's clock; a lapsed lease reads as released with no
timer. Each save renews it.
_Lives in_: a task's `lease_expires_at`; the project's lease setting.
_Avoid_: timeout, reservation

**Replica**:
One of the copies of an item a consensus project sends, each labelled by a different person, who never sees a sibling's
work.
_Lives in_: a task's `replica_of`; `Task.holders`.
_Avoid_: copy, duplicate

**Consensus**:
A project's rule that each item is labelled independently by several people (its replicas, 1 to 5), after which a
manager picks one replica's work.
_Lives in_: the project's `consensus_n`; `Adjudication`.
_Avoid_: agreement, voting

**Adjudication**:
A manager's pick of one replica's work for a consensus item. It is a pick, never a blend.
_Lives in_: `annotator.projects.models.Adjudication`; `AdjudicationPanel`.
_Avoid_: merge

**Draft**:
A task's work in progress: its shapes and links, kept in the annotator's Postgres until the project is published.
_Lives in_: the `drafts` table; `AnnotatorStore.change_draft`; ADR 0004.
_Avoid_: annotations table, working table

**Revision**:
The number of a draft's current state. A save names the revision it was made on and is refused if the draft moved on.
_Lives in_: `Draft.revision`; `X-Annotations-Version`.
_Avoid_: version (a version is a Lance table's)

**History**:
The saves a draft keeps (its last 50 within 8 MiB), each with what undoes it, so a past save can be read back and a save
can be undone on the server.
_Lives in_: the `draft_history` table; the History panel; `POST /api/annotations/<unit>/revert`.
_Avoid_: versions, audit log

### Labels

**Shape**:
One label on an item's unit: a box, polygon, mask, point, line, time segment, whole-item tag or text span, with its
class, attributes and who made it.
_Lives in_: `annotator.projects.models.Shape`.
_Avoid_: annotation (in prose, for one shape), object

**Region**:
A shape drawn on a picture or a film frame, or on a recording's time line (a segment).
_Avoid_: area, box (a box is one kind of region)

**Tag**:
A shape about the whole item, with no geometry. The Label grid labels items with tags only.
_Lives in_: `shape_type: "tag"`.
_Avoid_: classification, item label

**Span**:
A shape over a stretch of text, by its start and end in code points, inside a text row.
_Lives in_: a `text` shape with `char_start` and `char_end`; `#lib/canvas/text`.
_Avoid_: highlight, entity (in prose)

**Link**:
A named edge from one shape to another in the same draft.
_Lives in_: `annotator.projects.models.Link`; the ontology's relations.
_Avoid_: relation (the ontology's word for a kind of link), edge

**Proposal**:
A shape a model made that no person has kept yet: from assist, or attached at send. It is shown as proposed until
someone keeps or rejects it.
_Lives in_: a shape with `status: "prediction"` and `source: "model:<name>"`; a task's `prediction` list.
_Avoid_: prediction (the code's word), suggestion, pre-annotation

**Assist**:
Asking a model, from the canvas, for proposals on the open item: a detector's text prompt or a segmenter's clicks.
_Lives in_: `POST /api/assist/<unit>`; `#lib/canvas/AssistPanel.svelte`; `runners/assist`.
_Avoid_: auto-label (that is a batch job, which belongs to flows), AI

**Model endpoint**:
An address the workbench is given and calls for a model's answer. The workbench deploys no model.
_Avoid_: model server (the workbench runs none), backend

**Model version**:
Which model, at which version, made a proposal. Every proposal carries it, so two versions can be compared.
_Avoid_: checkpoint, run

**Judge**:
A model that assesses labels people made on an item and flags the ones it disagrees with, for a reviewer. It decides
nothing: a reviewer accepts or sends back.
_Avoid_: AI reviewer, auto-review, validator

**Sampling**:
Choosing which items are labelled next, or which items enter a project at all, by a strategy a project sets: the items
a model is least sure about, the items least like those already labelled, or a mix. A person can always override it.
_Avoid_: active learning (the training side is not the workbench's), smart queue

**Learning loop**:
People's kept, changed and removed proposals become published labels; a pipeline outside the workbench trains a model
on them; the new model version answers at the model endpoint. The workbench publishes and shows versions; it trains
nothing.
_Avoid_: reinforcement learning (no policy or reward is involved), retraining (the workbench does none)

### Screens

**Canvas**:
The page where one task's item is labelled: a picture, a recording or film on one time line, or a text.
_Lives in_: `/projects/[project]/tasks/[task]`; `#lib/canvas`.
_Avoid_: editor, labeling page

**Items**:
A project's tab that lists, filters, sorts and pages its items, with counts per state and per person and actions on many
at once.
_Lives in_: `DataManager.svelte`; `GET /projects/{id}/tasks`.
_Avoid_: data manager (the code's name), queue, task list

**Label grid**:
A project's Label tab: a keyboard grid of the items open to the reader, labelled with tags by digit, many at a time,
one save per item, undone on the server.
_Lives in_: `LabelGrid.svelte`, `label-grid.ts`, `label-run.ts`.
_Avoid_: bulk grid, bulk screen

**Browse**:
A project's tab, and the read-only pass over the items that hold work, opened on the canvas.
_Lives in_: `?pass=browse`; `BrowsePanel.svelte`.
_Avoid_: gallery

**Review pass**:
A reviewer's walk through the items waiting for review, one at a time on the canvas, with a verdict on each by button
or key.
_Lives in_: `?pass=review`; the Review tab.
_Avoid_: review queue

**Verdict**:
A reviewer's answer on an item in review: Accept, Fix and accept (accept with the reviewer's own changes), or Send back
(with a note, to the person who submitted it).
_Lives in_: the `accept`, `fix_and_accept` and `request_changes` events; `VERDICTS` in `#lib/projects/presentation.ts`.
_Avoid_: request changes and "Accept with my fixes" (both shown in the UI today; renames to make), approve, reject

**Publish**:
Writing a frozen project's accepted work out of the workbench, once, as a whole. Today the one target is a table in the
lakehouse, written as the workbench's own client into `<warehouse>$published`; a Lance table on S3 and export files are
wanted too.
_Lives in_: `annotator.projects.lakehouse.PublishRunner`; the Publish tab; ADR 0013.
_Avoid_: export (one of the targets), sync, promote

**Activity log**:
The bell: the signed-in person's own writes in this tab, each a success or a failure with its reason. It holds nothing of
anyone else's.
_Lives in_: `#lib/shell/activity-center.svelte`.
_Avoid_: notifications, inbox

### Look

**Appearance**:
A person's choice of look, kept in this browser: a mode (light, dark or the system's), a theme, and optionally an accent
colour of their own, corrected until it reads.
_Lives in_: the account menu; `#lib/appearance-options.ts`; ADR 0017.
_Avoid_: skin, branding

**Theme**:
One of the shipped colour sets, each in light and dark: Ink (the default), Fjord, Paper, Moss, Contrast.
_Lives in_: `@riksarkivet/ui`'s themes; `data-theme` on `<html>`.
_Avoid_: palette

**Design system**:
`@riksarkivet/ui`: the tokens, themes, components and the one Storybook, in its own repository, which the app pins by
commit. `@workbench/ui` is its frozen old copy that only the old apps import.
_Lives in_: `AI-Riksarkivet/ui`; ADRs 0015, 0016.
_Avoid_: component library (for the frozen copy), UI kit

### Parked

**Flows**:
Model pipelines over datasets, with an editor in the old studio and explorer apps and a service that runs them. Parked:
no screen and no deployment until labeling works on real data with a real model.
_Lives in_: `services/flows`; `frontend/studio`; `frontend/explorer`'s `/workflow`; ADR 0011.
_Avoid_: workflows, studio (the old app's name)
