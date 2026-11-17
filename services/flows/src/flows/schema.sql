-- The flows service's own state: each run and the outcome of each of its steps. Applied at every start, inside one
-- transaction holding an advisory lock (`flows.store.apply_schema`), so it is written to be re-run. `$run_states` and
-- `$step_states` are filled from `flows.models` at apply time, so the checks below cannot drift from the models.

CREATE TABLE IF NOT EXISTS runs (
    run_id      text        PRIMARY KEY,
    project     text        NOT NULL,
    subject     text        NOT NULL,
    state       text        NOT NULL,
    -- How many nodes the graph holds: the run ends in the transaction that records its last step.
    node_count  integer     NOT NULL CHECK (node_count > 0),
    -- What the run executes, whole (`flows.models.RunJob`): the graph, its seeds and the resolved Serve origin.
    job         jsonb       NOT NULL,
    -- The digest of the request that started the run (`flows.models.RunRequest.digest`): a replay of its
    -- Idempotency-Key is judged against this, never against `job` read back, which the store may hand back changed
    -- (a float past 2**53 returns as another number).
    request_digest text     NOT NULL,
    error       text,
    created_at  timestamptz NOT NULL,
    finished_at timestamptz,
    -- The driver's lease: which process drives the run, and until when, against the database clock. A driver claims it
    -- in one statement when it is absent or expired, renews it while it drives and releases it when it stops; one that
    -- stops renewing (its process died) loses it at its expiry, and any replica's re-drive claims it.
    owner       text,
    lease_until timestamptz,
    -- How many times a driver claimed the run, and when it last did. Every claim counts one attempt, so a run whose
    -- drivers keep stopping before it ends fails for good at its bound (`flows.runner`); the last claim orders the
    -- claim, so a run re-driven again and again waits behind runs that waited longer.
    attempts        integer     NOT NULL DEFAULT 0,
    last_claimed_at timestamptz,
    CHECK ((owner IS NULL) = (lease_until IS NULL)),
    CHECK ((state = 'running') = (finished_at IS NULL))
);
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_state_known;
ALTER TABLE runs ADD CONSTRAINT runs_state_known CHECK (state IN ($run_states));
-- The re-drive finds the runs no live driver holds, the one that waited longest first.
CREATE INDEX IF NOT EXISTS runs_unfinished ON runs ((coalesce(last_claimed_at, created_at)), run_id) WHERE state = 'running';
-- The retention finds the runs that ended longest ago.
CREATE INDEX IF NOT EXISTS runs_finished ON runs (finished_at) WHERE finished_at IS NOT NULL;

-- One row per step that has an outcome, written in the transaction that checks the driver still holds the run: a step
-- with a row is never executed again, by this driver or by any re-drive.
CREATE TABLE IF NOT EXISTS run_steps (
    run_id      text        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    node_id     text        NOT NULL,
    state       text        NOT NULL,
    -- What the builder paints on the node (`flows.models.NodeRunState`).
    outcome     jsonb       NOT NULL,
    -- What the step hands its dependents, capped by `flows.models.NodeResult`.
    payload     text,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
ALTER TABLE run_steps DROP CONSTRAINT IF EXISTS run_steps_state_known;
ALTER TABLE run_steps ADD CONSTRAINT run_steps_state_known CHECK (state IN ($step_states));
