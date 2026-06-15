from sqlalchemy import create_engine, text

from omai.clients.well_timeline_client import (
    UnavailableWellTimelineClient,
    WellTimelineClient,
    WellTimelineClientError,
)


def make_timeline_client() -> WellTimelineClient:
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
                CREATE TABLE well_tests (
                    id INTEGER PRIMARY KEY,
                    well_id INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    oil REAL,
                    water REAL,
                    gas REAL,
                    pip REAL,
                    m_temp REAL,
                    amps REAL,
                    tbgp REAL,
                    csgp REAL,
                    runtime REAL,
                    comments TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE well_fluids (
                    id INTEGER PRIMARY KEY,
                    well_id INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    level REAL,
                    comments TEXT
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
                CREATE TABLE general_notes (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    comments TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE work_orders (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    subject TEXT,
                    status TEXT,
                    priority INTEGER,
                    vendor TEXT,
                    comments TEXT
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO wells (id, site_id, name) VALUES (1, 1, '11-1-1 Oil')")
        )
        connection.execute(
            text("INSERT INTO wells (id, site_id, name) VALUES (2, 1, 'HARTZOG DRAW UNIT 4048')")
        )
        connection.execute(
            text(
                """
                INSERT INTO well_tests
                    (well_id, time, oil, water, gas, runtime, comments)
                VALUES
                    (1, '2026-05-09 08:00:00', 10, 2, 50, 24, 'good test')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO well_fluids (well_id, time, level, comments)
                VALUES (1, '2026-05-09 09:00:00', 1234, 'fluid shot')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO well_shutdowns
                    (well_id, date, hours, long_shutdown, downtime_code, comments)
                VALUES (1, '2026-05-10', 4.5, 0, 'PRF', 'paraffin')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO well_shutdowns
                    (well_id, date, hours, long_shutdown, downtime_code, comments)
                VALUES (2, '2026-06-01', 8, 0, 'DH', '4048 shutdown')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO general_notes (site_id, date, comments)
                VALUES (1, '2026-05-10', 'Checked 11-1-1 Oil after shutdown')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO work_orders
                    (site_id, time, subject, status, priority, vendor, comments)
                VALUES
                    (1, '2026-05-10 12:00:00', 'Repair 11-1-1 Oil', 'open', 3, 'Vendor A', 'replace part')
                """
            )
        )
    return WellTimelineClient(engine)


def test_get_well_timeline_returns_chronological_events():
    result = make_timeline_client().get_well_timeline(
        1, "11-1-1", "2026-05-09", "2026-05-10"
    )

    assert result["well"] == "Well - 11-1-1 Oil"
    assert [event["source"] for event in result["events"]] == [
        "well_test",
        "well_fluid",
        "shutdown",
        "general_note",
        "work_order",
    ]
    assert result["events"][0]["details"]["oil"] == 10
    assert result["events"][2]["details"]["downtime_reason"] == "Paraffin"
    assert result["events"][2]["time"] == "2026-05-10"
    assert result["events"][4]["details"]["subject"] == "Repair 11-1-1 Oil"


def test_numeric_well_name_resolves_hartzog_draw_unit_prefix():
    result = make_timeline_client().get_well_timeline(
        1, "4048", "2026-01-01", "2026-06-15"
    )

    assert result["well"] == "Well - HARTZOG DRAW UNIT 4048"
    assert result["events"] == [
        {
            "time": "2026-06-01",
            "source": "shutdown",
            "label": "Well shutdown",
            "details": {
                "type": "short",
                "downtime_code": "DH",
                "downtime_reason": "Downhole Problems",
                "comments": "4048 shutdown",
                "date": "2026-06-01",
                "hours": 8.0,
            },
        }
    ]


def test_well_timeline_rejects_unknown_well():
    try:
        make_timeline_client().get_well_timeline(
            1, "missing", "2026-05-09", "2026-05-10"
        )
    except WellTimelineClientError as exc:
        assert "Well not found" in str(exc)
    else:
        raise AssertionError("Expected WellTimelineClientError")


def test_unavailable_well_timeline_client_rejects_query():
    client = UnavailableWellTimelineClient("DB settings missing")
    try:
        client.get_well_timeline(1, "11-1-1", "2026-05-09", "2026-05-10")
    except WellTimelineClientError as exc:
        assert "DB settings missing" in str(exc)
    else:
        raise AssertionError("Expected WellTimelineClientError")
