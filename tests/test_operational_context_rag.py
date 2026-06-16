from datetime import date

from sqlalchemy import MetaData, create_engine, text

from omai.rag.document_models import RagDocument, chunk_document
from omai.rag.extractors.operational_text import OperationalTextExtractor
from omai.rag.vector_store import (
    _batched,
    _chunk_row,
    _extra_metadata,
    _pgvector_extension_exists,
    _rag_chunks_table,
    _search_row_to_match,
)
from omai.tools.operational_context_tools import build_operational_context_tools


class FakeOperationalContextStore:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "query": kwargs["query"],
            "count": 1,
            "matches": [
                {
                    "source_type": "general_note",
                    "source_id": "1",
                    "site_id": kwargs["site_id"],
                    "event_date": "2026-05-10",
                    "entity_name": None,
                    "text": "General note. Comments: Pump issue mentioned.",
                }
            ],
        }


class FakeExtensionConnection:
    def __init__(self, exists):
        self.exists = exists
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return self

    def mappings(self):
        return self

    def one(self):
        return {
            "database": "ometrics",
            "user": "ometrics",
            "server_addr": "127.0.0.1",
            "server_port": 5432,
            "installed": self.exists,
            "extension_schema": "public" if self.exists else None,
            "type_visible": self.exists,
        }


def test_chunk_document_keeps_metadata_and_stable_ids():
    document = RagDocument(
        source_type="general_note",
        source_id="123",
        site_id=4,
        event_date=date(2026, 5, 10),
        text="A" * 50,
    )

    chunks = chunk_document(document, max_chars=20, overlap_chars=5)

    assert len(chunks) > 1
    assert chunks[0].chunk_id == "general_note:123:chunk:0"
    assert chunks[0].metadata["site_id"] == 4
    assert chunks[0].metadata["event_date"] == "2026-05-10"
    assert chunks[0].metadata["event_date_key"] == 20260510


def test_vector_store_promotes_chunk_metadata_to_columns():
    chunk = chunk_document(
        RagDocument(
            source_type="general_note",
            source_id="123",
            site_id=4,
            event_date=date(2026, 5, 10),
            text="Pump issue mentioned.",
            metadata={"title": "General note"},
        )
    )[0]

    row = _chunk_row(chunk.metadata)

    assert row["source_type"] == "general_note"
    assert row["source_id"] == "123"
    assert row["site_id"] == 4
    assert row["event_date"] == date(2026, 5, 10)
    assert row["event_date_key"] == 20260510
    assert _extra_metadata(chunk.metadata) == {"title": "General note"}


def test_vector_store_table_uses_configured_embedding_dimensions():
    table = _rag_chunks_table(MetaData(), 1536)

    assert table.c.embedding.type.dim == 1536
    assert "idx_rag_chunks_site_date" in {index.name for index in table.indexes}
    assert "idx_rag_chunks_source" in {index.name for index in table.indexes}


def test_vector_store_batches_large_upserts():
    rows = [{"id": value} for value in range(5)]

    assert list(_batched(rows, 2)) == [
        [{"id": 0}, {"id": 1}],
        [{"id": 2}, {"id": 3}],
        [{"id": 4}],
    ]


def test_vector_store_search_row_format_matches_tool_contract():
    row = {
        "chunk_id": "general_note:1:chunk:0",
        "source_type": "general_note",
        "source_id": "1",
        "site_id": 4,
        "event_date": date(2026, 6, 10),
        "entity_type": None,
        "entity_id": None,
        "entity_name": None,
        "distance": 0.123,
        "text": "General note. Comments: In range note.",
    }

    assert _search_row_to_match(row) == {
        "chunk_id": "general_note:1:chunk:0",
        "source_type": "general_note",
        "source_id": "1",
        "site_id": 4,
        "event_date": "2026-06-10",
        "entity_type": None,
        "entity_id": None,
        "entity_name": None,
        "distance": 0.123,
        "text": "General note. Comments: In range note.",
    }


def test_pgvector_extension_check_does_not_create_extension():
    connection = FakeExtensionConnection(exists=False)

    assert _pgvector_extension_exists(connection) is False
    assert "CREATE EXTENSION" not in connection.statements[0]
    assert "current_database()" in connection.statements[0]
    assert "pg_extension" in connection.statements[0]


def test_operational_context_tool_calls_store_with_site_scope():
    store = FakeOperationalContextStore()
    tool = build_operational_context_tools(store, site_id=4)[0]

    result = tool.invoke(
        {
            "query": "Why was the well down?",
            "start_date": "2026-05-01",
            "end_date": "2026-05-31",
            "entity_name": "HARTZOG DRAW UNIT 4048",
            "source_types": ["general_note"],
            "limit": 3,
        }
    )

    assert '"ok":true' in result
    assert store.calls[0]["site_id"] == 4
    assert store.calls[0]["start_date"] == date(2026, 5, 1)
    assert store.calls[0]["end_date"] == date(2026, 5, 31)
    assert store.calls[0]["entity_name"] == "HARTZOG DRAW UNIT 4048"


def test_operational_context_tool_accepts_compact_yyyymmdd_dates():
    store = FakeOperationalContextStore()
    tool = build_operational_context_tools(store, site_id=4)[0]

    result = tool.invoke(
        {
            "query": "Summarize general notes.",
            "start_date": "20260601",
            "end_date": "20260630",
            "source_types": ["general_note"],
        }
    )

    assert '"ok":true' in result
    assert store.calls[0]["start_date"] == date(2026, 6, 1)
    assert store.calls[0]["end_date"] == date(2026, 6, 30)


def test_operational_text_extractor_reads_general_notes_without_mysql_writes():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE general_notes (
                    id INTEGER PRIMARY KEY,
                    comments TEXT NOT NULL,
                    site_id INTEGER NOT NULL,
                    date DATE NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO general_notes (id, comments, site_id, date, updated_at)
                VALUES (1, 'Pump issue mentioned.', 4, '2026-05-10', '2026-05-10 12:00:00')
                """
            )
        )

    documents = OperationalTextExtractor(engine).extract(
        site_id=4,
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
        source_types={"general_note"},
    )

    assert len(documents) == 1
    assert documents[0].source_type == "general_note"
    assert documents[0].site_id == 4
    assert documents[0].text == "General note. Comments: Pump issue mentioned."
