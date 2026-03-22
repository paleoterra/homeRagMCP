"""
Brain CLI — create, list, remove, and ingest into brain databases.

Usage:
  uv run brain create <name> [options]
  uv run brain list
  uv run brain remove <name> [--delete-data]
  uv run brain ingest <directory> --name <brain-name> [options]

Each brain becomes an MCP server entry in the Claude Desktop config, namespaced
as "brain-<name>". Tools appear in Claude as brain-<name>/search, etc.

Examples:
  uv run brain create personal --embedder mlx
  uv run brain create work-docs --embedder mlx
  uv run brain create archive --backend postgres --pg-url postgresql://brain:brain@nas:5432
  uv run brain list
  uv run brain remove archive
  uv run brain remove temp-research --delete-data
  uv run brain ingest ~/Documents/Notes --name personal
  uv run brain ingest ~/Documents/Reports --name work-docs --tags work,reports --recursive
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="brain",
    help="Manage brain databases and Claude Desktop MCP registrations.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BRAIN_DIR = Path(__file__).parent.parent.resolve()
CLAUDE_CONFIG = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
)
DEFAULT_SQLITE_DIR = Path.home() / ".brain"

# ---------------------------------------------------------------------------
# Embedder env blocks
# ---------------------------------------------------------------------------

EMBEDDER_ENV = {
    "mlx": {
        "EMBEDDER_BACKEND": "mlx",
        "MLX_MODEL": "mlx-community/nomic-embed-text-v1.5",
    },
    "ollama": {
        "EMBEDDER_BACKEND": "ollama",
        "OLLAMA_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "nomic-embed-text",
    },
    "nlembedding": {
        "EMBEDDER_BACKEND": "nlembedding",
        "VECTOR_DIMENSIONS": "512",
    },
    "openai": {
        "EMBEDDER_BACKEND": "openai",
        "OPENAI_EMBED_MODEL": "text-embedding-3-small",
        "OPENAI_EMBED_DIMENSIONS": "768",
        # OPENAI_API_KEY must be set in the environment separately
    },
}

# ---------------------------------------------------------------------------
# Claude Desktop config helpers
# ---------------------------------------------------------------------------


def _load_claude_config() -> dict:
    if CLAUDE_CONFIG.exists():
        return json.loads(CLAUDE_CONFIG.read_text())
    return {}


def _save_claude_config(config: dict) -> None:
    CLAUDE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_CONFIG.write_text(json.dumps(config, indent=2))


def _mcp_key(name: str) -> str:
    return f"brain-{name}"


def _mcp_entry(database_url: str, embedder: str) -> dict:
    env = {"DATABASE_URL": database_url}
    env.update(EMBEDDER_ENV.get(embedder, EMBEDDER_ENV["mlx"]))
    return {
        "command": "uv",
        "args": [
            "--directory", str(BRAIN_DIR),
            "run", "python", "-m", "brain.server",
        ],
        "env": env,
    }


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------


def _postgres_create(pg_admin_url: str, db_name: str) -> None:
    """Create a Postgres database and run migrations."""
    import asyncpg
    import asyncio

    # Build the target DB URL by appending the db name to the admin URL
    db_url = f"{pg_admin_url.rstrip('/')}/{db_name}"

    async def _run():
        conn = await asyncpg.connect(pg_admin_url)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if exists:
                typer.echo(f"  Database '{db_name}' already exists.")
            else:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
                typer.echo(f"  Created database '{db_name}'.")
        finally:
            await conn.close()

        # Run migrations (all use IF NOT EXISTS — safe to re-run)
        conn2 = await asyncpg.connect(db_url)
        try:
            migrations = sorted((BRAIN_DIR / "migrations").glob("*.sql"))
            migrations = [m for m in migrations if "512" not in m.name]
            for migration in migrations:
                typer.echo(f"  Applying {migration.name} …")
                await conn2.execute(migration.read_text())
        finally:
            await conn2.close()

    asyncio.run(_run())


def _postgres_drop(pg_admin_url: str, db_name: str) -> None:
    import asyncpg
    import asyncio

    async def _run():
        conn = await asyncpg.connect(pg_admin_url)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            typer.echo(f"  Dropped database '{db_name}'.")
        finally:
            await conn.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def create(
    name: str = typer.Argument(..., help="Short name for the brain, e.g. 'personal' or 'work-docs'"),
    backend: str = typer.Option("sqlite", "--backend", "-b", help="Database backend: sqlite or postgres"),
    embedder: str = typer.Option("mlx", "--embedder", "-e", help="Embedding backend: mlx, ollama, nlembedding, openai"),
    sqlite_dir: Path = typer.Option(DEFAULT_SQLITE_DIR, "--sqlite-dir", help="Directory for SQLite .db files"),
    pg_url: str = typer.Option(
        "postgresql://brain:brain@localhost:5432",
        "--pg-url",
        help="Postgres admin connection URL (without database name)",
    ),
) -> None:
    """Create a new brain database and register it with Claude Desktop."""
    key = _mcp_key(name)
    config = _load_claude_config()
    servers = config.setdefault("mcpServers", {})

    if key in servers:
        typer.echo(f"Error: '{key}' is already registered. Use 'brain remove {name}' first.")
        raise typer.Exit(1)

    if embedder not in EMBEDDER_ENV:
        typer.echo(f"Error: unknown embedder '{embedder}'. Choose from: {', '.join(EMBEDDER_ENV)}")
        raise typer.Exit(1)

    typer.echo(f"Creating brain '{name}' …")

    if backend == "sqlite":
        sqlite_dir.mkdir(parents=True, exist_ok=True)
        db_path = sqlite_dir / f"{name}.db"
        database_url = f"sqlite://{db_path}"
        typer.echo(f"  SQLite database: {db_path}")
        typer.echo("  (Schema will be created automatically on first use.)")

    elif backend == "postgres":
        db_name = f"brain_{name.replace('-', '_')}"
        typer.echo(f"  Creating Postgres database '{db_name}' …")
        try:
            _postgres_create(pg_url, db_name)
        except Exception as e:
            typer.echo(f"Error connecting to Postgres: {e}")
            raise typer.Exit(1)
        database_url = f"{pg_url.rstrip('/')}/{db_name}"

    else:
        typer.echo(f"Error: unknown backend '{backend}'. Choose 'sqlite' or 'postgres'.")
        raise typer.Exit(1)

    servers[key] = _mcp_entry(database_url, embedder)
    _save_claude_config(config)

    typer.echo(f"\nRegistered as '{key}' in Claude Desktop config.")
    typer.echo("Restart Claude Desktop to activate.")


@app.command(name="list")
def list_brains() -> None:
    """List all registered brain databases."""
    config = _load_claude_config()
    servers = config.get("mcpServers", {})
    brains = {k: v for k, v in servers.items() if k.startswith("brain-")}

    if not brains:
        typer.echo("No brain databases registered.")
        return

    typer.echo(f"{'NAME':<20} {'BACKEND':<10} {'EMBEDDER':<14} DATABASE")
    typer.echo("-" * 80)
    for key, entry in sorted(brains.items()):
        env = entry.get("env", {})
        db_url = env.get("DATABASE_URL", "?")
        backend = "sqlite" if db_url.startswith("sqlite://") else "postgres"
        embedder = env.get("EMBEDDER_BACKEND", "ollama")
        short_db = db_url.replace(str(Path.home()), "~")
        name = key.removeprefix("brain-")
        typer.echo(f"{name:<20} {backend:<10} {embedder:<14} {short_db}")


@app.command()
def remove(
    name: str = typer.Argument(..., help="Name of the brain to remove"),
    delete_data: bool = typer.Option(False, "--delete-data", help="Also delete the database file/schema"),
    pg_url: str = typer.Option(
        "postgresql://brain:brain@localhost:5432",
        "--pg-url",
        help="Postgres admin URL (needed only if --delete-data and backend is postgres)",
    ),
) -> None:
    """Deregister a brain from Claude Desktop. Optionally delete its data."""
    key = _mcp_key(name)
    config = _load_claude_config()
    servers = config.get("mcpServers", {})

    if key not in servers:
        typer.echo(f"Error: '{key}' is not registered.")
        raise typer.Exit(1)

    entry = servers[key]
    env = entry.get("env", {})
    db_url = env.get("DATABASE_URL", "")

    if delete_data:
        if db_url.startswith("sqlite://"):
            db_path = Path(db_url.removeprefix("sqlite://"))
            if db_path.exists():
                db_path.unlink()
                typer.echo(f"  Deleted {db_path}")
            else:
                typer.echo(f"  File not found: {db_path}")
        else:
            # Postgres
            db_name = db_url.rsplit("/", 1)[-1]
            try:
                _postgres_drop(pg_url, db_name)
            except Exception as e:
                typer.echo(f"  Warning: could not drop Postgres database: {e}")

    del servers[key]
    _save_claude_config(config)
    typer.echo(f"Removed '{key}' from Claude Desktop config.")
    typer.echo("Restart Claude Desktop to apply.")


@app.command()
def migrate(
    name: str = typer.Argument(..., help="Brain name to migrate (e.g. 'shared')"),
) -> None:
    """Apply database migrations to an existing Postgres brain."""
    import asyncpg
    import asyncio

    key = _mcp_key(name)
    config = _load_claude_config()
    servers = config.get("mcpServers", {})

    if key not in servers:
        typer.echo(f"Error: '{key}' is not registered. Run 'brain create {name}' first.")
        raise typer.Exit(1)

    db_url = servers[key].get("env", {}).get("DATABASE_URL", "")
    if db_url.startswith("sqlite://"):
        typer.echo("SQLite schema is created automatically on first use — nothing to do.")
        return

    async def _run():
        conn = await asyncpg.connect(db_url)
        try:
            migrations = sorted((BRAIN_DIR / "migrations").glob("*.sql"))
            migrations = [m for m in migrations if "512" not in m.name]
            for migration in migrations:
                typer.echo(f"  Applying {migration.name} …")
                await conn.execute(migration.read_text())
        finally:
            await conn.close()

    typer.echo(f"Running migrations on '{key}' …")
    try:
        asyncio.run(_run())
        typer.echo("Done.")
    except Exception as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(1)


@app.command()
def ingest(
    directory: Path = typer.Argument(..., help="Directory to ingest files from"),
    name: str = typer.Option(..., "--name", "-n", help="Brain name to ingest into (e.g. 'shared', 'personal')"),
    tags: str = typer.Option("", "--tags", "-t", help="Comma-separated tags to apply to all files"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Recurse into subdirectories"),
    force: bool = typer.Option(False, "--force", help="Re-ingest files already in the database"),
    extensions: str = typer.Option(
        ".pdf,.md,.txt,.docx,.pptx,.xlsx,.numbers,.epub",
        "--extensions",
        help="Comma-separated list of file extensions to include",
    ),
) -> None:
    """Ingest all supported files from a directory into a brain database."""
    import asyncio
    from brain.ingestion.pipeline import ingest_file

    key = _mcp_key(name)
    config = _load_claude_config()
    servers = config.get("mcpServers", {})

    if key not in servers:
        typer.echo(f"Error: '{key}' is not registered. Run 'brain create {name}' first.")
        raise typer.Exit(1)

    env = servers[key].get("env", {})
    database_url = env.get("DATABASE_URL", "")
    embedder_backend = env.get("EMBEDDER_BACKEND", "ollama")

    path_arg = directory.expanduser().resolve()
    if not path_arg.exists():
        typer.echo(f"Error: '{path_arg}' does not exist.")
        raise typer.Exit(1)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ext_set = {e.strip().lower() for e in extensions.split(",") if e.strip()}

    if path_arg.is_file():
        files = [path_arg]
        directory = path_arg.parent
    else:
        directory = path_arg
        # Collect files
        glob = "**/*" if recursive else "*"
        files = [p for p in directory.glob(glob) if p.is_file() and p.suffix.lower() in ext_set]

    if not files:
        typer.echo(f"No supported files found in {path_arg}")
        raise typer.Exit(0)

    typer.echo(f"Found {len(files)} file(s)")
    typer.echo(f"Brain: {key}  Embedder: {embedder_backend}  Tags: {tag_list or 'none'}\n")

    async def _run() -> None:
        # Build DB
        if database_url.startswith("sqlite://"):
            from brain.db_sqlite import SqliteDatabase
            dimensions = int(env.get("VECTOR_DIMENSIONS", "768"))
            db = SqliteDatabase(database_url.removeprefix("sqlite://"), dimensions=dimensions)
        else:
            from brain.db import Database
            db = Database(database_url)
        await db.connect()

        # Build embedder
        from brain.embedder import Embedder
        embedder = Embedder(
            backend=embedder_backend,
            base_url=env.get("OLLAMA_URL", "http://localhost:11434"),
            model=env.get("OLLAMA_MODEL", "nomic-embed-text"),
            mlx_model=env.get("MLX_MODEL", "mlx-community/nomic-embed-text-v1.5"),
            openai_model=env.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            openai_dimensions=int(env.get("OPENAI_EMBED_DIMENSIONS", "768")),
            openai_api_key=env.get("OPENAI_API_KEY", ""),
        )

        ingested = skipped = failed = 0

        for path in sorted(files):
            rel = path.relative_to(directory) if path.is_relative_to(directory) else path.name
            try:
                await ingest_file(db, embedder, str(path), tags=tag_list, force=force)
                typer.echo(f"  ✓  {rel}")
                ingested += 1
            except ValueError as e:
                msg = str(e)
                if "force=True" in msg:
                    typer.echo(f"  –  {rel}  (already indexed, use --force to re-ingest)")
                else:
                    typer.echo(f"  –  {rel}  ({msg})")
                skipped += 1
            except Exception as e:
                typer.echo(f"  ✗  {rel}  ({e})")
                failed += 1

        await db.disconnect()

        typer.echo(f"\nDone — {ingested} ingested, {skipped} skipped, {failed} failed.")

    asyncio.run(_run())


@app.command(name="google-auth")
def google_auth(
    secrets: Path = typer.Option(
        None,
        "--secrets",
        help="Path to client_secrets.json (overrides GOOGLE_CLIENT_SECRETS env var)",
    ),
    token_path: Path = typer.Option(
        None,
        "--token-path",
        help="Where to save the token (overrides GOOGLE_TOKEN_PATH env var)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-authenticate even if a valid token already exists"
    ),
) -> None:
    """Authenticate with Google (OAuth2) for Drive/Docs/Sheets/Slides access.

    Opens a browser to complete the OAuth2 consent flow, then saves a token
    for future use. Desktop environments only — use a service account for
    headless/SSH contexts.

    Requires: uv sync --extra google
    """
    import json
    import os

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        typer.echo("Error: Google extras not installed. Run: uv sync --extra google")
        raise typer.Exit(1)

    from brain.ingestion.parsers.google_drive import SCOPES, _DEFAULT_SECRETS, _DEFAULT_TOKEN

    secrets_path = secrets or Path(os.environ.get("GOOGLE_CLIENT_SECRETS", str(_DEFAULT_SECRETS)))
    tok_path = token_path or Path(os.environ.get("GOOGLE_TOKEN_PATH", str(_DEFAULT_TOKEN)))

    # Check if already authenticated
    if not force and tok_path.exists():
        try:
            data = json.loads(tok_path.read_text())
            creds = Credentials.from_authorized_user_info(data, SCOPES)
            if creds.valid:
                typer.echo(f"Already authenticated. Use --force to re-authenticate.")
                typer.echo(f"Token: {tok_path}")
                return
        except Exception:
            pass  # Token invalid or corrupt — proceed to re-auth

    # Check client secrets exist
    if not secrets_path.exists():
        typer.echo(f"Error: client_secrets.json not found at {secrets_path}")
        typer.echo(
            "Download it from Google Cloud Console → APIs & Services → "
            "Credentials → OAuth 2.0 Client ID → Download JSON"
        )
        raise typer.Exit(1)

    typer.echo("Opening browser for Google OAuth2 consent…")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as e:
        typer.echo(f"Authentication failed: {e}")
        raise typer.Exit(1)

    tok_path.parent.mkdir(parents=True, exist_ok=True)
    tok_path.write_text(creds.to_json())
    typer.echo(f"Authenticated successfully. Token saved to {tok_path}")


if __name__ == "__main__":
    app()
