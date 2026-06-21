from __future__ import annotations

from pathlib import Path


class DatabaseSchemaClientError(RuntimeError):
    """Raised when curated database schema context cannot be loaded."""


class DatabaseSchemaClient:
    """Loads the curated operational database schema document.

    The client deliberately reads a static markdown file only. It does not
    inspect the live database, so adding this client cannot expose extra tables
    beyond the allowlist documented in `knowledge/database_schema.md`.
    """

    def __init__(self, schema_path: Path | str):
        """Store the configured schema document path."""
        self.schema_path = Path(schema_path)

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


class UnavailableDatabaseSchemaClient:
    """Fallback client used when schema setup fails during tool wiring."""

    def __init__(self, reason: str):
        """Store the setup failure reason for later tool calls."""
        self.reason = reason

    def load_schema(self) -> str:
        """Raise the original setup reason when the tool is invoked."""
        raise DatabaseSchemaClientError(self.reason)
