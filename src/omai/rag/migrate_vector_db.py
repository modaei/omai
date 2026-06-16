from __future__ import annotations

from omai.config.settings import Settings
from omai.rag.vector_store import VectorOperationalContextStore


def main() -> None:
    settings = Settings.from_env()
    settings.validate()
    store = VectorOperationalContextStore.from_settings(settings)
    store.migrate()
    print("Vector DB schema is ready.")


if __name__ == "__main__":
    main()
