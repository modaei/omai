from sqlalchemy import create_engine, text

from omai.clients.shutdown_client import (
    DOWNTIME_CODES,
    ShutdownClient,
    ShutdownClientError,
    UnavailableShutdownClient,
)


def make_shutdown_client() -> ShutdownClient:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE wells (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE well_shutdowns (
                    id INTEGER PRIMARY KEY,
                    well_id INTEGER NOT NULL,
                    date TEXT,
                    hours REAL,
                    long_shutdown INTEGER NOT NULL DEFAULT 0,
                    long_shutdown_start TEXT,
                    long_shutdown_end TEXT,
                    downtime_code TEXT,
                    comments TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO wells (id, site_id, name)
                VALUES
                    (1, 1, '11-1-1 Oil'),
                    (2, 1, '12-2-1 Oil'),
                    (3, 2, 'Other Site Well')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO well_shutdowns
                    (
                        id,
                        well_id,
                        date,
                        hours,
                        long_shutdown,
                        long_shutdown_start,
                        long_shutdown_end,
                        downtime_code,
                        comments
                    )
                VALUES
                    (10, 1, '2026-05-09', 4.5, 0, NULL, NULL, 'PRF', 'paraffin cleanout'),
                    (11, 2, NULL, NULL, 1, '2026-05-01 08:00:00', NULL, 'DH', 'downhole issue'),
                    (12, 3, '2026-05-09', 24, 0, NULL, NULL, 'PRF', 'other site')
                """
            )
        )
    return ShutdownClient(engine)


def test_get_shutdowns_returns_short_and_long_shutdowns_for_site():
    result = make_shutdown_client().get_shutdowns(
        1, "2026-05-01", "2026-05-10"
    )

    assert result["short_count"] == 1
    assert result["long_count"] == 1
    assert result["short_total_hours"] == 4.5
    assert result["short_shutdowns"] == [
        {
            "well": "Well - 11-1-1 Oil",
            "type": "short",
            "date": "2026-05-09",
            "hours": 4.5,
            "downtime_code": "PRF",
            "downtime_reason": "Paraffin",
            "comments": "paraffin cleanout",
        }
    ]
    assert result["long_shutdowns"] == [
        {
            "well": "Well - 12-2-1 Oil",
            "type": "long",
            "start": "2026-05-01 08:00:00",
            "end": None,
            "downtime_code": "DH",
            "downtime_reason": "Downhole Problems",
            "comments": "downhole issue",
        }
    ]


def test_get_current_long_shutdowns():
    result = make_shutdown_client().get_current_long_shutdowns(1, "2026-05-10")

    assert result["count"] == 1
    assert result["long_shutdowns"][0]["well"] == "Well - 12-2-1 Oil"


def test_list_downtime_codes():
    result = make_shutdown_client().list_downtime_codes()

    assert result["downtime_codes"]["PRF"] == DOWNTIME_CODES["PRF"]


def test_shutdown_date_order_is_validated():
    try:
        make_shutdown_client().get_shutdowns(1, "2026-05-10", "2026-05-01")
    except ShutdownClientError as exc:
        assert "start_date cannot be after end_date" in str(exc)
    else:
        raise AssertionError("Expected ShutdownClientError")


def test_unavailable_shutdown_client_lists_codes_but_rejects_queries():
    client = UnavailableShutdownClient("DB settings missing")

    assert client.list_downtime_codes()["downtime_codes"]["PRF"] == "Paraffin"
    try:
        client.get_current_long_shutdowns(1, "2026-05-10")
    except ShutdownClientError as exc:
        assert "DB settings missing" in str(exc)
    else:
        raise AssertionError("Expected ShutdownClientError")
