#!/usr/bin/env bash
# The workbench's local stack: the viewer, search, annotator and flows with sign-in off, on a local lance-namespace
# directory catalog, and the built app behind its Gateway stand-in (`frontend/app/edge.ts`), on one origin. Everything
# listens on the loopback only: with sign-in off, nothing beyond this machine may reach it. It prints the one URL to
# open; Ctrl-C stops every process it started, and if any of them stops on its own the rest are stopped and the script
# says which.
#
# `make local` installs, builds the app and runs this with SEED=1, which seeds the catalog
# (`scripts/seed_demo_corpus.py`) once the ports are known to be free: a seed rewrites the tables a running stack holds
# open. Run on its own it expects the app built (`bun --cwd=frontend/app run build`) and seeds only with SEED=1.
#
#   MEDIA_CATALOG_DIR   the directory catalog every service reads (default: $TMPDIR/workbench-catalog);
#                       MEDIA_CATALOG_URI names a catalog's REST door instead
#   EDGE_PORT           the origin the browser uses (default 8700); serve.ts listens on EDGE_PORT + 1000
#   VIEWER_PORT, SEARCH_PORT, ANNOTATOR_PORT, FLOWS_PORT   the services' own ports (8701, 8702, 8703, 8704)
#   STATE_PORT          the annotator's PostgreSQL (default 8705), the pinned server `make postgres` exports
#   STATE_DIR           its data directory (default: the catalog directory's sibling `<catalog>-state`), kept between
#                       runs, so projects and tasks survive a restart of the stack
#
# Every port is checked before anything starts: one that something already listens on is refused, by name.
#
# The person is anonymous and administers the one local organisation (`annotator.api.v1.endpoints.me`). No sidecar
# runs, so flows runs its pipelines inline (`flows.lifespan`). No embedder runs either: full-text search works,
# semantic search does not.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOOPBACK=127.0.0.1

# The services are mounted under /api (their own default is /api/v1), the base the edge rewrites each public prefix to.
export WORKBENCH_API_PREFIX="${WORKBENCH_API_PREFIX:-/api}"
# A local stack signs nobody in, and says so the one way the services accept (authentication is on by default).
export WORKBENCH_OIDC_ENABLED=false
export WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED=true
if [ -z "${MEDIA_CATALOG_URI:-}" ]; then
  export MEDIA_CATALOG_DIR="${MEDIA_CATALOG_DIR:-${TMPDIR:-/tmp}/workbench-catalog}"
  mkdir -p "$MEDIA_CATALOG_DIR"
fi
export MEDIA_DEFAULT_DATASET="${MEDIA_DEFAULT_DATASET:-demo\$corpus}"

# Development credentials, in files as a deployment mounts them: the viewer's media-URL signing key (32 random bytes,
# base64), made once per host, and the token flows presents to Serve. A local Serve verifies no token, so the dev one is
# a placeholder; a deployment mounts a projected ServiceAccount token with audience `serve`.
DEV_SECRETS="${TMPDIR:-/tmp}/workbench-dev-secrets"
mkdir -p "$DEV_SECRETS" && chmod 700 "$DEV_SECRETS"
if [ ! -s "$DEV_SECRETS/media-url-key" ]; then
  (umask 077 && openssl rand -base64 32 > "$DEV_SECRETS/media-url-key")
fi
[ -s "$DEV_SECRETS/serve-token" ] || (umask 077 && echo "dev-unverified" > "$DEV_SECRETS/serve-token")
export VIEWER_MEDIA_URL_KEY_FILE="${VIEWER_MEDIA_URL_KEY_FILE:-$DEV_SECRETS/media-url-key}"
export WORKBENCH_FLOWS_SERVE_TOKEN_FILE="${WORKBENCH_FLOWS_SERVE_TOKEN_FILE:-$DEV_SECRETS/serve-token}"

EDGE_PORT="${EDGE_PORT:-8700}"
STATE_PORT="${STATE_PORT:-8705}"
STATE_DIR="${STATE_DIR:-${MEDIA_CATALOG_DIR:-${TMPDIR:-/tmp}/workbench}-state}"
POSTGRES_TREE="${WORKBENCH_POSTGRES_DIR:-$ROOT/.localbin/postgres}"
VIEWER_PORT="${VIEWER_PORT:-8701}"
SEARCH_PORT="${SEARCH_PORT:-8702}"
ANNOTATOR_PORT="${ANNOTATOR_PORT:-8703}"
FLOWS_PORT="${FLOWS_PORT:-8704}"
APP_CONFIG="$ROOT/frontend/app/dev/config.json"

if [ ! -x "$POSTGRES_TREE/bin/postgres" ]; then
  echo "no PostgreSQL server at $POSTGRES_TREE: run \`make postgres\` (or \`make local\`)" >&2
  exit 1
fi
if [ ! -f "$ROOT/frontend/app/build/index.html" ]; then
  echo "the app is not built: run \`bun --cwd=frontend/app run build\` (or \`make local\`)" >&2
  exit 1
fi

# A port something already answers on would serve another program's pages under this stack's URL: refuse it, by name.
busy=()
for named in "edge:$EDGE_PORT" "app:$((EDGE_PORT + 1000))" "viewer:$VIEWER_PORT" "search:$SEARCH_PORT" \
  "annotator:$ANNOTATOR_PORT" "flows:$FLOWS_PORT" "state:$STATE_PORT"; do
  port=${named#*:}
  if (exec 3<>"/dev/tcp/$LOOPBACK/$port") 2>/dev/null; then busy+=("$port (${named%%:*})"); fi
done
if [ ${#busy[@]} -gt 0 ]; then
  echo "not starting: something already listens on port ${busy[*]}. Stop it, or choose other ports (EDGE_PORT, VIEWER_PORT, SEARCH_PORT, ANNOTATOR_PORT, FLOWS_PORT, STATE_PORT)." >&2
  exit 1
fi

if [ "${SEED:-}" = 1 ]; then
  (cd "$ROOT" && uv run --no-sync python scripts/seed_demo_corpus.py)
fi

# Each child in a process group of its own (job control), so stopping one stops its whole pipeline, and the terminal's
# Ctrl-C reaches this script, which stops them, and not the children directly.
set -m
declare -A NAMES=()

run() {  # run <name> <command...>
  local name=$1
  shift
  ( "$@" 2>&1 | sed -u "s/^/[$name] /" ) &
  NAMES[$!]=$name
}

# Stop every child's process group and wait until each is gone (10 s, then KILL), so the ports are free when this
# returns: `wait` alone returns at once when a trap interrupted a `wait -n`.
stop() {
  set +m  # no job notices while the children end
  # PostgreSQL takes INT as a fast shutdown (sessions ended, a clean checkpoint); TERM would wait for every client.
  for pid in "${!NAMES[@]}"; do
    if [ "${NAMES[$pid]}" = state ]; then kill -INT -- "-$pid" 2>/dev/null || true; else kill -TERM -- "-$pid" 2>/dev/null || true; fi
  done
  for pid in "${!NAMES[@]}"; do
    for _ in $(seq 1 50); do
      kill -0 -- "-$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL -- "-$pid" 2>/dev/null || true
  done
}

trap 'echo; echo "stopping the local stack..."; trap - INT TERM; stop; exit 0' INT TERM

cd "$ROOT"

# The annotator's state: the pinned PostgreSQL on the loopback, trusting only the loopback, its data kept in STATE_DIR.
# Started first and waited for, since the annotator refuses to start without the database it names.
pg() { LD_LIBRARY_PATH="$POSTGRES_TREE/deps" "$POSTGRES_TREE/bin/$1" "${@:2}"; }
if [ ! -f "$STATE_DIR/PG_VERSION" ]; then
  mkdir -p "$STATE_DIR"
  pg initdb -D "$STATE_DIR" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
fi
run state env LD_LIBRARY_PATH="$POSTGRES_TREE/deps" "$POSTGRES_TREE/bin/postgres" -D "$STATE_DIR" -p "$STATE_PORT" \
  -c listen_addresses="$LOOPBACK" -c unix_socket_directories=
for _ in $(seq 1 100); do
  pg pg_isready -q -h "$LOOPBACK" -p "$STATE_PORT" -U postgres && break
  sleep 0.1
done
if ! pg pg_isready -q -h "$LOOPBACK" -p "$STATE_PORT" -U postgres; then
  echo "the annotator's PostgreSQL did not start within 10 s; stopping" >&2
  trap - INT TERM
  stop
  exit 1
fi
if [ "$(pg psql -h "$LOOPBACK" -p "$STATE_PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'annotator'")" != 1 ]; then
  pg createdb -h "$LOOPBACK" -p "$STATE_PORT" -U postgres annotator
fi
export ANNOTATOR_STATE_HOST="$LOOPBACK" ANNOTATOR_STATE_PORT="$STATE_PORT" ANNOTATOR_STATE_DATABASE=annotator \
  ANNOTATOR_STATE_USER=postgres ANNOTATOR_STATE_SSLMODE=disable

run viewer uv run --no-sync uvicorn viewer.main:app --host "$LOOPBACK" --port "$VIEWER_PORT"
run search uv run --no-sync uvicorn search.main:app --host "$LOOPBACK" --port "$SEARCH_PORT"
run annotator uv run --no-sync uvicorn annotator.main:app --host "$LOOPBACK" --port "$ANNOTATOR_PORT"
run flows uv run --no-sync uvicorn flows:app --host "$LOOPBACK" --port "$FLOWS_PORT"
run edge env -C "$ROOT/frontend/app" PORT="$EDGE_PORT" HOST="$LOOPBACK" WORKBENCH_APP_CONFIG_FILE="$APP_CONFIG" \
  VIEWER_API="http://$LOOPBACK:$VIEWER_PORT" SEARCH_API="http://$LOOPBACK:$SEARCH_PORT" ANNOTATOR_API="http://$LOOPBACK:$ANNOTATOR_PORT" \
  bun edge.ts

# Ready when each service answers its own liveness probe and the edge serves this stack's own /config.json, which
# another program on the edge's port would not.
ready() {
  for port in "$VIEWER_PORT" "$SEARCH_PORT" "$ANNOTATOR_PORT" "$FLOWS_PORT"; do
    curl -fsS -o /dev/null "http://$LOOPBACK:$port/livez" 2>/dev/null || return 1
  done
  [ "$(curl -fsS "http://$LOOPBACK:$EDGE_PORT/config.json" 2>/dev/null)" = "$(cat "$APP_CONFIG")" ]
}
stopped_while_starting() {
  for pid in "${!NAMES[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "the ${NAMES[$pid]} stopped while starting; stopping the rest" >&2
      return 0
    fi
  done
  return 1
}
up=false
for _ in $(seq 1 120); do
  if ready; then up=true && break; fi
  if stopped_while_starting; then break; fi
  sleep 0.5
done
if [ "$up" != true ]; then
  [ "$(jobs -rp | wc -l)" -lt "${#NAMES[@]}" ] || echo "the local stack did not become ready within 60 s; stopping it" >&2
  trap - INT TERM
  stop
  exit 1
fi

# Loopback only, checked rather than assumed: a listener this stack opened on any other address is refused.
if command -v ss >/dev/null; then
  for port in "$EDGE_PORT" "$((EDGE_PORT + 1000))" "$VIEWER_PORT" "$SEARCH_PORT" "$ANNOTATOR_PORT" "$FLOWS_PORT" "$STATE_PORT"; do
    wide=$(ss -ltnH "sport = :$port" | awk '{print $4}' | grep -v "^$LOOPBACK:" || true)
    if [ -n "$wide" ]; then
      echo "port $port listens beyond the loopback ($wide); stopping the local stack" >&2
      trap - INT TERM
      stop
      exit 1
    fi
  done
fi

echo
echo "  the workbench is up: http://$LOOPBACK:$EDGE_PORT/datasets"
echo

# Any child that stops ends the stack: the rest are stopped, and the script names which one and fails.
set +e
wait -n -p stopped
status=$?
trap - INT TERM
echo "the ${NAMES[${stopped:-0}]:-stack} stopped (exit $status); stopping the rest" >&2
stop
exit 1
