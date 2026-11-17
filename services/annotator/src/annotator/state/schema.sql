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
CREATE INDEX IF NOT EXISTS tasks_of_group ON tasks (project_id, replica_of, task_id) WHERE replica_of IS NOT NULL;

-- One draft per task: its work in progress, the whole shape set, replaced on each save, guarded by its revision.
CREATE TABLE IF NOT EXISTS drafts (
    task_id  text PRIMARY KEY REFERENCES tasks (task_id) ON DELETE CASCADE,
    revision integer NOT NULL,
    document jsonb   NOT NULL
);

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

-- Each task's state as a reader sees it: a claim past its expiry is unassigned. The one statement of that rule in SQL;
-- `annotator.projects.machines.lapse_expired_claim` is the same rule on a document, against the same clock.
CREATE OR REPLACE VIEW task_states AS
SELECT task_id,
       project_id,
       CASE WHEN state = 'claimed' AND lease_expires_at <= clock_timestamp() THEN 'unassigned' ELSE state END AS state
FROM tasks;
