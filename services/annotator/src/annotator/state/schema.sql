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
-- A STORED VALUE IS NEVER STALE, by construction rather than by care. Postgres computes a stored generated column when a
-- row is written and never again, so two edits would leave rows written before them holding what an older definition
-- computed, with nothing to say so: changing a column's expression here (`ADD COLUMN IF NOT EXISTS` keeps the one the
-- database holds), and changing the body of a function a column calls (Postgres uses a replaced body only for rows written
-- after it). So:
--   * every generated column is declared through `annotator_declare_generated`, which compares the expression and type
--     the database holds (`pg_attrdef`, deparsed by `pg_get_expr`) with the declared ones, deparsed the same way, and drops
--     and adds the column again when they differ, which computes it for every row; when they match it changes nothing;
--   * a function a column calls is NAMED BY ITS BODY: `annotator_declare_function` names it `<base>_<the first 12 hex of
--     the SHA-256 of its body>`, the body read with carriage returns removed, every run of whitespace made one space and
--     the ends trimmed, creates it when absent, and leaves it alone when present (the name is the body). A column's
--     expression names a function by its base in double braces (`{{annotator_instant}}`), which the declaration fills in
--     with that name. So edit a body freely: a cosmetic edit (indentation, line endings, spacing) keeps its name and
--     changes nothing, and a real one gives a new name, so the columns that call it converge, computing every row once.
--     A run of whitespace inside a string literal of a body is read as one space too: a change there alone is not seen.
--     Every function named by a base this file declares and no longer current is dropped once the columns have moved on.
-- Each convergence drops a column and adds it again, which takes one of a table's 1,600 column numbers for good (a
-- dropped column keeps its number), so it must happen only on a real change; the comparisons above make it so.
-- All of it runs inside the schema's transaction under its advisory lock (`apply_schema`), so a start converges alone.
CREATE OR REPLACE FUNCTION annotator_declare_function(base text, args text, returns text, body text) RETURNS text
    LANGUAGE plpgsql AS $$fn$$
DECLARE
    name text := base || '_' || left(encode(sha256(convert_to(btrim(regexp_replace(replace(body, chr(13), ''), '\s+', ' ', 'g')), 'UTF8')), 'hex'), 12);
BEGIN
    IF to_regprocedure(format('%I(%s)', name, args)) IS NULL THEN
        EXECUTE format('CREATE FUNCTION %I(%s) RETURNS %s LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS %L', name, args, returns, body);
    END IF;
    PERFORM set_config('annotator.fn.' || base, name, true);
    RETURN name;
END
$$fn$$;
-- Drop every function named by ``base`` (itself, or `<base>_<suffix>`) but the current one: run once no column calls them.
CREATE OR REPLACE FUNCTION annotator_drop_superseded(base text) RETURNS void
    LANGUAGE plpgsql AS $$fn$$
DECLARE
    superseded regprocedure;
BEGIN
    FOR superseded IN
        SELECT p.oid::regprocedure FROM pg_proc p
        WHERE p.pronamespace = current_schema()::regnamespace
          AND (p.proname = base OR p.proname ~ ('^' || base || '_[0-9a-z]+$$'))
          AND p.proname <> current_setting('annotator.fn.' || base)
    LOOP
        EXECUTE format('DROP FUNCTION %s', superseded);
    END LOOP;
END
$$fn$$;
CREATE OR REPLACE FUNCTION annotator_declare_generated(tbl text, col text, typ text, expr text) RETURNS void
    LANGUAGE plpgsql AS $$fn$$
DECLARE
    present boolean := false;
    held text;
    wanted text;
    called text;
BEGIN
    -- The functions it calls, by the names their bodies give them in this apply.
    FOR called IN SELECT DISTINCT m[1] FROM regexp_matches(expr, '\{\{([a-z_0-9]+)\}\}', 'g') AS m LOOP
        expr := replace(expr, '{{' || called || '}}', current_setting('annotator.fn.' || called));
    END LOOP;
    SELECT true, format_type(a.atttypid, a.atttypmod) || ' ' || coalesce(a.attgenerated::text, '') || ' ' || coalesce(pg_get_expr(d.adbin, d.adrelid), '')
        INTO present, held
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = to_regclass(tbl) AND a.attname = col AND NOT a.attisdropped;
    -- The declared column deparsed the way the held one is: on a scratch table with the same `document`.
    EXECUTE format('CREATE TEMP TABLE annotator_declared (document jsonb, declared %s GENERATED ALWAYS AS (%s) STORED)', typ, expr);
    SELECT format_type(a.atttypid, a.atttypmod) || ' ' || a.attgenerated::text || ' ' || pg_get_expr(d.adbin, d.adrelid) INTO wanted
        FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = 'pg_temp.annotator_declared'::regclass AND a.attname = 'declared';
    DROP TABLE pg_temp.annotator_declared;
    IF held IS DISTINCT FROM wanted THEN
        IF present THEN
            EXECUTE format('ALTER TABLE %I DROP COLUMN %I', tbl, col);
        END IF;
        EXECUTE format('ALTER TABLE %I ADD COLUMN %I %s GENERATED ALWAYS AS (%s) STORED', tbl, col, typ, expr);
    END IF;
END
$$fn$$;

-- An instant in the document is read only when it is written as ISO 8601 with an explicit offset
-- (`2026-11-10T09:30:00.123456Z`, `...+01:00`), as the store writes every instant it stamps (the database's clock, aware).
-- Such a text names one instant whatever the session's TimeZone or DateStyle, so the cast below is truly immutable, which
-- a generated column needs: a text of any other shape (no offset, `now`, a day-first date) would depend on the session,
-- so it reads as no instant (NULL: it sorts last and falls outside every range) rather than as a guess.
SELECT annotator_declare_function('annotator_instant', 'text', 'timestamptz', $$body$$SELECT CASE WHEN $$1 ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$$' THEN $$1::timestamptz END$$body$$);
SELECT annotator_declare_generated('tasks', 'dataset', 'text', $$x$$document #>> '{source,where}'$$x$$);
SELECT annotator_declare_generated('tasks', 'submitted_by', 'text', $$x$$document ->> 'submitted_by'$$x$$);
SELECT annotator_declare_generated('tasks', 'reviewed_by', 'text', $$x$$document ->> 'reviewed_by'$$x$$);
SELECT annotator_declare_generated('tasks', 'review_action', 'text', $$x$$document ->> 'review_action'$$x$$);
SELECT annotator_declare_generated('tasks', 'holders', 'jsonb', $$x$$coalesce(document -> 'holders', '[]'::jsonb)$$x$$);
SELECT annotator_declare_generated('tasks', 'predicted', 'jsonb', $$x$$jsonb_path_query_array(document, '$$.prediction[*].label')$$x$$);
SELECT annotator_declare_generated('tasks', 'sent_at', 'timestamptz', $$x$${{annotator_instant}}(document ->> 'sent_at')$$x$$);
SELECT annotator_declare_generated('tasks', 'submitted_at', 'timestamptz', $$x$${{annotator_instant}}(document ->> 'submitted_at')$$x$$);
SELECT annotator_declare_generated('tasks', 'reviewed_at', 'timestamptz', $$x$${{annotator_instant}}(document ->> 'reviewed_at')$$x$$);
-- The last transition's instant, or the send's for an item nobody has touched.
SELECT annotator_declare_generated('tasks', 'updated_at', 'timestamptz', $$x$$coalesce(
    {{annotator_instant}}(jsonb_path_query_first(document, '$$.transitions[last].at') #>> '{}'), {{annotator_instant}}(document ->> 'sent_at')
)$$x$$);
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
SELECT annotator_declare_generated('drafts', 'labels', 'jsonb', $$x$$jsonb_path_query_array(document, '$$.shapes[*].label')$$x$$);
-- Whether the draft holds any work (a shape or a link): what a browse of the labeled items filters on.
SELECT annotator_declare_generated('drafts', 'has_work', 'boolean', $$x$$jsonb_path_exists(document, '$$.shapes[0]') OR jsonb_path_exists(document, '$$.links[0]')$$x$$);
-- The draft's whole-item tags (its `tag` shapes) as a listing answers them for a bulk label (`task_query.work_of`), in the
-- draft's order: each one's id and class, whether it carries more than its class (text, attributes, or a link to or from
-- it), which a new label would discard, and whether it is a proposal no person has kept yet (a `prediction`). Only these,
-- so a tag-heavy draft's row grows by a few dozen bytes a tag, never by a second copy of its shapes. The links' ends are
-- read once, as a set, so a draft at its caps costs one pass over its links, not one per tag.
SELECT annotator_declare_function('annotator_work_tags', 'jsonb', 'jsonb', $$body$$WITH ends AS (
         SELECT end_id FROM jsonb_array_elements(coalesce($$1 -> 'links', '[]'::jsonb)) AS l(link),
             LATERAL (VALUES (link ->> 'from_shape'), (link ->> 'to_shape')) AS v(end_id)
     )
     SELECT coalesce(jsonb_agg(jsonb_build_object(
         'id', t ->> 'shape_id',
         'label', t -> 'label',
         'detailed', coalesce(t ->> 'text', '') <> ''
             OR coalesce(t -> 'attributes', '{}'::jsonb) NOT IN ('{}'::jsonb, 'null'::jsonb)
             OR (t ->> 'shape_id') IN (SELECT end_id FROM ends),
         'prediction', coalesce(t ->> 'status', '') = 'prediction'
     ) ORDER BY n), '[]'::jsonb)
     FROM jsonb_array_elements(coalesce($$1 -> 'shapes', '[]'::jsonb)) WITH ORDINALITY AS e(t, n)
     WHERE t ->> 'shape_type' = 'tag'$$body$$);
SELECT annotator_declare_generated('drafts', 'work_tags', 'jsonb', $$x$${{annotator_work_tags}}(document)$$x$$);

-- The functions no column calls any more: the earlier bodies of each.
SELECT annotator_drop_superseded('annotator_instant');
SELECT annotator_drop_superseded('annotator_work_tags');
-- A column a development build once carried (each tag shape whole) and nothing reads.
ALTER TABLE drafts DROP COLUMN IF EXISTS tags;

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
