from __future__ import annotations

import argparse
from datetime import date

from omai.config.settings import Settings
from omai.rag.extractors.operational_text import OperationalTextExtractor
from omai.rag.indexer import OperationalContextIndexer
from omai.rag.vector_store import VectorOperationalContextStore


def main() -> None:
    # This command is intentionally manual/simple for the first RAG slice. It can
    # later be wrapped by cron, a queue worker, or an Ometrics admin action.
    parser = argparse.ArgumentParser(
        description="Index Ometrics text records into the vector DB."
    )
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--from-date", dest="start_date")
    parser.add_argument("--to-date", dest="end_date")
    parser.add_argument("--source-type", action="append", dest="source_types")
    parser.add_argument(
        "--reset-site",
        action="store_true",
        help="Delete existing indexed chunks for the selected site/source types before indexing.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    # validate() checks LLM/embedding settings, while validate_database() checks
    # the source MySQL connection used by OperationalTextExtractor.
    settings.validate()
    settings.validate_database()

    indexer = OperationalContextIndexer(
        extractor=OperationalTextExtractor.from_settings(settings),
        store=VectorOperationalContextStore.from_settings(settings),
    )
    indexer.store.migrate()
    if args.reset_site:
        deleted = indexer.store.delete_site(
            args.site_id,
            set(args.source_types) if args.source_types else None,
        )
        print(f"Deleted {deleted} existing indexed chunks.")
    result = indexer.index(
        site_id=args.site_id,
        start_date=date.fromisoformat(args.start_date) if args.start_date else None,
        end_date=date.fromisoformat(args.end_date) if args.end_date else None,
        source_types=set(args.source_types) if args.source_types else None,
    )
    # The command reports vector DB chunks, not MySQL writes. No RAG state is
    # stored in the Ometrics database.
    print(f"Indexed {result.documents} documents into {result.chunks} vector chunks.")


if __name__ == "__main__":
    main()
