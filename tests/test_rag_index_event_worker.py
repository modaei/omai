import logging

from omai.rag.process_index_events import RagIndexEventWorker
from omai.repositories.rag_index_event_repository import RagIndexEvent


class FakeRepository:
    def __init__(self, events):
        self.events = events
        self.deleted = []
        self.failed = []

    def claim_batch(self):
        return self.events

    def delete_completed(self, event_id):
        self.deleted.append(event_id)

    def mark_failed(self, event, error):
        self.failed.append((event, error))

    def database_name(self):
        return "ometrics"


class FakeIndexer:
    def __init__(self, fail=False):
        self.fail = fail
        self.indexed = []
        self.deleted = []

    def index_source(self, site_id, source_type, source_id):
        if self.fail:
            raise RuntimeError("embedding provider failed")
        self.indexed.append((site_id, source_type, source_id))

    def delete_source(self, site_id, source_type, source_id):
        if self.fail:
            raise RuntimeError("vector delete failed")
        self.deleted.append((site_id, source_type, source_id))


def event(**overrides):
    values = {
        "id": 10,
        "site_id": 4,
        "source_type": "general_note",
        "source_id": "123",
        "operation": "updated",
        "status": "pending",
        "attempts": 0,
    }
    values.update(overrides)
    return RagIndexEvent(**values)


def test_worker_indexes_event_and_deletes_completed_outbox_row():
    repository = FakeRepository([event()])
    indexer = FakeIndexer()
    worker = RagIndexEventWorker(repository, indexer)

    assert worker.run_once() == 1

    assert indexer.indexed == [(4, "general_note", "123")]
    assert repository.deleted == [10]
    assert repository.failed == []


def test_worker_deletes_vector_chunks_for_deleted_event():
    repository = FakeRepository([event(operation="deleted")])
    indexer = FakeIndexer()
    worker = RagIndexEventWorker(repository, indexer)

    assert worker.run_once() == 1

    assert indexer.deleted == [(4, "general_note", "123")]
    assert repository.deleted == [10]


def test_worker_marks_failed_event_and_logs_failure(caplog):
    repository = FakeRepository([event()])
    indexer = FakeIndexer(fail=True)
    worker = RagIndexEventWorker(repository, indexer)

    with caplog.at_level(logging.ERROR):
        assert worker.run_once() == 1

    assert repository.deleted == []
    assert len(repository.failed) == 1
    assert "embedding provider failed" in repository.failed[0][1]
    assert "RAG index event vectorization failed." in caplog.text


def test_worker_logs_when_previously_failed_event_succeeds(caplog):
    repository = FakeRepository([event(status="failed", attempts=2)])
    indexer = FakeIndexer()
    worker = RagIndexEventWorker(repository, indexer)

    with caplog.at_level(logging.INFO):
        assert worker.run_once() == 1

    assert repository.deleted == [10]
    assert "Previously failed RAG index event succeeded." in caplog.text


def test_worker_returns_zero_when_no_events_are_claimed():
    repository = FakeRepository([])
    indexer = FakeIndexer()
    worker = RagIndexEventWorker(repository, indexer)

    assert worker.run_once() == 0
