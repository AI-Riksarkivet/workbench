-- The annotator's own state: its projects, the tasks sent into them, each task's draft. Applied at every start, inside
-- one transaction holding an advisory lock (`annotator.state.store.apply_schema`), so it is written to be re-run.
-- `$project_states` and `$task_states` are filled from the `ProjectState` and `TaskState` enums at apply time, so the
-- checks below cannot drift from the models; a literal dollar sign is written `$$`.
--
-- Each row holds its Pydantic document whole (`document`), beside the few columns a query filters, sorts or locks on.
-- The document is authoritative; every column is written from it in the same statement.

CREATE TABLE IF NOT EXISTS projects (
    project_id          text        PRIMARY KEY,
    tenant              text        NOT NULL,
    state               text        NOT NULL,
    created_at          timestamptz NOT NULL,
    -- The publish run's lease: which process drives the project's publish, and until when. A run claims it in one
    -- statement against the database clock when it is absent or expired, renews it between the saga's steps and
    -- releases it when it ends; a run that stops renewing loses it at its expiry, and any replica's re-drive claims it.
    publish_owner       text,
    publish_lease_until timestamptz,
    document            jsonb       NOT NULL,
    CHECK ((publish_owner IS NULL) = (publish_lease_until IS NULL))
);
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_state_known;
ALTER TABLE projects ADD CONSTRAINT projects_state_known CHECK (state IN ($project_states));
-- An organisation's landing lists its projects oldest first.
CREATE INDEX IF NOT EXISTS projects_of_tenant ON projects (tenant, created_at, project_id);
-- The re-drive finds the publishes no live run holds.
CREATE INDEX IF NOT EXISTS projects_publishing ON projects (publish_lease_until) WHERE state = 'publishing';

-- A consensus group belongs to exactly one project: every replica names its group and its project together, and the
-- foreign key on `tasks` admits only the pair this table holds.
CREATE TABLE IF NOT EXISTS replica_groups (
    replica_of text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    UNIQUE (replica_of, project_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id          text PRIMARY KEY,
    project_id       text NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    state            text NOT NULL,
    -- A claim is its holder and its expiry: a claim past its expiry is read as released (`lease_expires_at` against
    -- the database clock), and the `lease_expired` edge is written the next time the row is locked.
    assignee         text,
    lease_expires_at timestamptz,
    -- The consensus group a replica belongs to (`{group}-r{k}`); a claim locks every replica of its group.
    replica_of       text,
    document         jsonb NOT NULL,
    FOREIGN KEY (replica_of, project_id) REFERENCES replica_groups (replica_of, project_id),
    CHECK (
        replica_of IS NULL
        OR (left(task_id, length(replica_of) + 2) = replica_of || '-r' AND substr(task_id, length(replica_of) + 3) ~ '^[0-9]+$$')
    )
);
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_state_known;
ALTER TABLE tasks ADD CONSTRAINT tasks_state_known CHECK (state IN ($task_states));
CREATE INDEX IF NOT EXISTS tasks_of_project ON tasks (project_id, task_id);

-- What the data manager filters and sorts a project's items on, read from the document by Postgres itself: a stored
-- generated column is recomputed on every write of the row and computed for every row already there when it is added,
-- so the document stays the one truth and no write path can leave a column behind it. A listing reads these narrow
-- columns and never the (toasted) document of a row it does not answer.
--
-- An instant in the document is read only when it is written as ISO 8601 with an explicit offset
-- (`2026-11-10T09:30:00.123456Z`, `...+01:00`), as the store writes every instant it stamps (the database's clock, aware).
-- Such a text names one instant whatever the session's TimeZone or DateStyle, so the cast below is truly immutable, which
-- a generated column needs: a text of any other shape (no offset, `now`, a day-first date) would depend on the session,
-- so it reads as no instant (NULL: it sorts last and falls outside every range) rather than as a guess.
CREATE OR REPLACE FUNCTION annotator_instant(value text) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS
    'SELECT CASE WHEN $$1 ~ ''^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$$'' THEN $$1::timestamptz END';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dataset text GENERATED ALWAYS AS (document #>> '{source,where}') STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS submitted_by text GENERATED ALWAYS AS (document ->> 'submitted_by') STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewed_by text GENERATED ALWAYS AS (document ->> 'reviewed_by') STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS review_action text GENERATED ALWAYS AS (document ->> 'review_action') STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS holders jsonb GENERATED ALWAYS AS (coalesce(document -> 'holders', '[]'::jsonb)) STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS predicted jsonb
    GENERATED ALWAYS AS (jsonb_path_query_array(document, '$$.prediction[*].label')) STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sent_at timestamptz GENERATED ALWAYS AS (annotator_instant(document ->> 'sent_at')) STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS submitted_at timestamptz GENERATED ALWAYS AS (annotator_instant(document ->> 'submitted_at')) STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewed_at timestamptz GENERATED ALWAYS AS (annotator_instant(document ->> 'reviewed_at')) STORED;
-- The last transition's instant, or the send's for an item nobody has touched.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at timestamptz GENERATED ALWAYS AS (
    coalesce(annotator_instant(jsonb_path_query_first(document, '$$.transitions[last].at') #>> '{}'), annotator_instant(document ->> 'sent_at'))
) STORED;
-- The filter by dataset offers the project's datasets (`SELECT DISTINCT dataset … WHERE project_id`): read off this
-- index rather than the project's rows. The listing's other filters and sorts read the project's rows through
-- `tasks_of_project` (measured on 100,000 items in one project: tens of milliseconds a statement, `task_query`).
CREATE INDEX IF NOT EXISTS tasks_by_dataset ON tasks (project_id, dataset);
CREATE INDEX IF NOT EXISTS tasks_of_group ON tasks (project_id, replica_of, task_id) WHERE replica_of IS NOT NULL;

-- One draft per task: its work in progress, the whole shape set, replaced on each save, guarded by its revision.
CREATE TABLE IF NOT EXISTS drafts (
    task_id  text PRIMARY KEY REFERENCES tasks (task_id) ON DELETE CASCADE,
    revision integer NOT NULL,
    document jsonb   NOT NULL
);
-- The classes the draft's shapes carry, for the data manager's filter by class (generated as the task's columns are).
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS labels jsonb GENERATED ALWAYS AS (jsonb_path_query_array(document, '$$.shapes[*].label')) STORED;
-- Whether the draft holds any work (a shape or a link): what a browse of the labeled items filters on.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS has_work boolean
    GENERATED ALWAYS AS (jsonb_path_exists(document, '$$.shapes[0]') OR jsonb_path_exists(document, '$$.links[0]')) STORED;

-- Each task's last saves: who saved each revision and when, how many shapes it left, and what undoes it (the shapes as
-- they were before it for every shape it changed or removed, the ids it added, and the links before it when it changed
-- them), written in the transaction that writes the revision. A past save is rebuilt from the draft by undoing the saves
-- after it (`annotator.state.store.AnnotatorStore.draft_at`). Bounded per task in saves and in bytes
-- (`MAX_DRAFT_HISTORY`, `MAX_HISTORY_BYTES`): the oldest past the bound is deleted in that same transaction.
CREATE TABLE IF NOT EXISTS draft_history (
    task_id  text        NOT NULL REFERENCES tasks (task_id) ON DELETE CASCADE,
    revision integer     NOT NULL,
    saved_at timestamptz NOT NULL,
    author   text        NOT NULL,
    shapes   integer     NOT NULL,
    bytes    integer     NOT NULL,
    undo     jsonb       NOT NULL,
    PRIMARY KEY (task_id, revision)
);

-- Each task's state as a reader sees it: a claim past its expiry is unassigned. `annotator.projects.machines.lapse_expired_claim`
-- is the same rule on a document, and `annotator.state.task_query` the same rule over the listing's columns. The clock is
-- the transaction's (`now()`), so every statement of one repeatable-read answer reads a lapse at one instant.
CREATE OR REPLACE VIEW task_states AS
SELECT task_id,
       project_id,
       CASE WHEN state = 'claimed' AND lease_expires_at <= now() THEN 'unassigned' ELSE state END AS state
FROM tasks;

-- The name each person's own verified token carried the last time they wrote through a door (`annotator.people`): what
-- the app shows for a principal, the principal itself its tooltip. Never a name a request body states. Bounded where it
-- is read from the token (`people.display_name`) and again here.
CREATE TABLE IF NOT EXISTS people (
    principal text        PRIMARY KEY CHECK (char_length(principal) BETWEEN 1 AND 512),
    name      text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    seen_at   timestamptz NOT NULL
);
