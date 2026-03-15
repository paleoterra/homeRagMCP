#!/usr/bin/env bash
# apple-container.sh — manage brain containers using Apple's container CLI
#
# Requirements: macOS 26+, Apple container tool installed
#   Install: download from https://github.com/apple/container/releases
#            or: brew install --cask container
#
# Usage:
#   ./scripts/apple-container.sh up       Start containers (creates on first run)
#   ./scripts/apple-container.sh down     Stop containers (data preserved)
#   ./scripts/apple-container.sh reset    Wipe data and recreate from scratch
#   ./scripts/apple-container.sh status   Show container status
#   ./scripts/apple-container.sh psql     Open a psql shell
#   ./scripts/apple-container.sh logs     Follow PostgreSQL logs

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRAIN_DIR="$HOME/workspace/brain"
DATA_DIR="$BRAIN_DIR/data/postgres"
MIGRATIONS_DIR="$BRAIN_DIR/migrations"

DB_NAME="brain-db"
PGADMIN_NAME="brain-pgadmin"

DB_IMAGE="docker.io/pgvector/pgvector:pg16"
PGADMIN_IMAGE="docker.io/dpage/pgadmin4:latest"

DB_PORT=5432
PGADMIN_PORT=5050

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}→${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
error() { echo -e "${RED}✗${NC}  $*" >&2; }

require_container_cli() {
    if ! command -v container &>/dev/null; then
        error "'container' CLI not found."
        echo "  Install from: https://github.com/apple/container/releases"
        echo "  Or: brew install --cask container"
        exit 1
    fi
}

# Returns 0 if the named container exists (running or stopped)
container_exists() {
    container list --all --format json 2>/dev/null | grep -q "\"$1\""
}

# Returns 0 if the named container is currently running
container_is_running() {
    container list --format json 2>/dev/null | grep -q "\"$1\""
}

wait_for_postgres() {
    info "Waiting for PostgreSQL to be ready..."
    # Give the container a moment to initialise its log file before probing.
    sleep 2
    local attempts=0
    until container exec "$DB_NAME" pg_isready -U brain -d brain -q 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
            error "PostgreSQL did not become ready after 30 seconds."
            error "Check logs with: ./scripts/apple-container.sh logs"
            exit 1
        fi
        sleep 1
    done
    info "PostgreSQL is ready."
}

# ---------------------------------------------------------------------------
# Start the Apple container system (idempotent — safe to call every time)
# ---------------------------------------------------------------------------

ensure_system_started() {
    info "Ensuring Apple container system is running..."
    # container system start is idempotent; prints a warning if already started
    container system start 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_up() {
    require_container_cli
    ensure_system_started

    mkdir -p "$DATA_DIR"

    # -- PostgreSQL --
    if container_is_running "$DB_NAME"; then
        warn "$DB_NAME is already running."
    elif container_exists "$DB_NAME"; then
        info "Starting existing $DB_NAME container..."
        container start "$DB_NAME"
        wait_for_postgres
    else
        info "Creating $DB_NAME (first run — migrations will apply automatically)..."
        container run \
            --name "$DB_NAME" \
            --detach \
            -p "${DB_PORT}:5432" \
            -e POSTGRES_DB=brain \
            -e POSTGRES_USER=brain \
            -e POSTGRES_PASSWORD=brain \
            --volume "$DATA_DIR":/var/lib/postgresql/data \
            --volume "$MIGRATIONS_DIR":/docker-entrypoint-initdb.d \
            "$DB_IMAGE"
        wait_for_postgres
    fi

    # -- pgAdmin --
    if container_is_running "$PGADMIN_NAME"; then
        warn "$PGADMIN_NAME is already running."
    elif container_exists "$PGADMIN_NAME"; then
        info "Starting existing $PGADMIN_NAME container..."
        container start "$PGADMIN_NAME"
    else
        info "Creating $PGADMIN_NAME..."
        container run \
            --name "$PGADMIN_NAME" \
            --detach \
            -p "${PGADMIN_PORT}:80" \
            -e PGADMIN_DEFAULT_EMAIL=admin@brain.local \
            -e PGADMIN_DEFAULT_PASSWORD=admin \
            "$PGADMIN_IMAGE"
    fi

    echo ""
    info "PostgreSQL : localhost:${DB_PORT}"
    info "pgAdmin   : http://localhost:${PGADMIN_PORT}"
}

cmd_down() {
    require_container_cli
    info "Stopping containers..."

    if container_is_running "$DB_NAME"; then
        container stop "$DB_NAME"
        info "Stopped $DB_NAME"
    else
        warn "$DB_NAME is not running"
    fi

    if container_is_running "$PGADMIN_NAME"; then
        container stop "$PGADMIN_NAME"
        info "Stopped $PGADMIN_NAME"
    else
        warn "$PGADMIN_NAME is not running"
    fi
}

cmd_reset() {
    require_container_cli
    warn "This will permanently delete all data in $DATA_DIR"
    read -rp "Type 'yes' to continue: " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi

    # Stop running containers
    container stop "$DB_NAME" 2>/dev/null || true
    container stop "$PGADMIN_NAME" 2>/dev/null || true

    # Delete containers (so they can be recreated cleanly)
    container delete --force "$DB_NAME" 2>/dev/null     && info "Deleted $DB_NAME"     || true
    container delete --force "$PGADMIN_NAME" 2>/dev/null && info "Deleted $PGADMIN_NAME" || true

    # Wipe host data directory
    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        info "Wiped $DATA_DIR"
    fi

    info "Reset complete. Run 'up' to start fresh."
}

cmd_status() {
    require_container_cli
    echo "=== Running ==="
    container list 2>/dev/null || echo "  (container system not started)"
    echo ""
    echo "=== All (including stopped) ==="
    container list --all 2>/dev/null || true
}

cmd_psql() {
    require_container_cli
    if ! container_is_running "$DB_NAME"; then
        error "$DB_NAME is not running. Start it with: ./scripts/apple-container.sh up"
        exit 1
    fi
    container exec --tty --interactive "$DB_NAME" psql -U brain -d brain
}

cmd_logs() {
    require_container_cli
    container logs --follow "$DB_NAME"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-help}" in
    up)     cmd_up     ;;
    down)   cmd_down   ;;
    reset)  cmd_reset  ;;
    status) cmd_status ;;
    psql)   cmd_psql   ;;
    logs)   cmd_logs   ;;
    *)
        echo "Usage: $(basename "$0") <command>"
        echo ""
        echo "Commands:"
        echo "  up      Create and start containers (idempotent)"
        echo "  down    Stop containers, preserve data"
        echo "  reset   Destroy all data and containers"
        echo "  status  Show container status"
        echo "  psql    Open an interactive psql shell"
        echo "  logs    Follow PostgreSQL logs"
        echo ""
        echo "Requirements: macOS 26+, Apple container CLI"
        echo "  https://github.com/apple/container/releases"
        ;;
esac
