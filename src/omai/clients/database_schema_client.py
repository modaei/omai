from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings
from omai.tools.operational_sql_validator import ALLOWED_TABLES


class DatabaseSchemaClientError(RuntimeError):
    """Raised when curated database schema context cannot be loaded."""


class DatabaseSchemaClient:
    """Loads the curated operational database schema document.

    The client deliberately reads a static markdown file only. It does not
    inspect the live database, so adding this client cannot expose extra tables
    beyond the allowlist documented in `knowledge/database_schema.md`.
    """

    def __init__(self, schema_path: Path | str, engine: Engine | None = None):
        """Store the configured schema document path and optional DB engine."""
        self.schema_path = Path(schema_path)
        self.engine = engine

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        schema_path: Path | str,
    ) -> "DatabaseSchemaClient":
        """Create a schema client backed by the configured Ometrics database.

        The engine is used only for metadata inspection of allowlisted tables.
        It does not execute LLM-generated SQL.
        """
        settings.validate_database()
        url = URL.create(
            drivername="mysql+pymysql",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        engine = create_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )
        return cls(schema_path, engine)

    def load_schema(self) -> str:
        """Return the schema markdown used by draft-only SQL tooling."""
        try:
            # Keep the schema source explicit and version-controlled.
            return self.schema_path.read_text(encoding="utf-8")
        except OSError as exc:
            # Convert filesystem failures into a domain-specific error so tools
            # can return structured JSON instead of leaking a traceback.
            raise DatabaseSchemaClientError(
                f"Could not load database schema document: {self.schema_path}"
            ) from exc

    def load_column_metadata(self) -> dict[str, set[str]]:
        """Return column names for allowlisted tables from database metadata."""
        if self.engine is None:
            raise DatabaseSchemaClientError("Database schema metadata is unavailable.")

        try:
            inspector = inspect(self.engine)
            existing_tables = set(inspector.get_table_names())
            metadata: dict[str, set[str]] = {}
            for table in sorted(ALLOWED_TABLES & existing_tables):
                # SQLAlchemy's inspector reads database metadata only; it does
                # not execute any LLM-drafted operational SQL.
                metadata[table] = {
                    str(column["name"]) for column in inspector.get_columns(table)
                }
            return metadata
        except SQLAlchemyError as exc:
            raise DatabaseSchemaClientError(
                f"Could not load database column metadata: {exc}"
            ) from exc


class UnavailableDatabaseSchemaClient:
    """Fallback client used when schema setup fails during tool wiring."""

    def __init__(self, reason: str):
        """Store the setup failure reason for later tool calls."""
        self.reason = reason

    def load_schema(self) -> str:
        """Raise the original setup reason when the tool is invoked."""
        raise DatabaseSchemaClientError(self.reason)

    def load_column_metadata(self) -> dict[str, set[str]]:
        """Raise the original setup reason when validation needs metadata."""
        raise DatabaseSchemaClientError(self.reason)
