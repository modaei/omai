from sqlalchemy import create_engine, text

from omai.clients.data_entry_client import DataEntryClient


def make_client():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE flares (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE flow_meters (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE pumps (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE treaters (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE knock_outs (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE water_plants (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE lacts (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT)"))
        connection.execute(text("CREATE TABLE tanks (id INTEGER PRIMARY KEY, site_id INTEGER, name TEXT, type TEXT)"))
        connection.execute(text("CREATE TABLE flare_readings (id INTEGER PRIMARY KEY, flare_id INTEGER, time TEXT)"))
        connection.execute(text("CREATE TABLE general_notes (id INTEGER PRIMARY KEY, site_id INTEGER, date TEXT)"))
        connection.execute(text("CREATE TABLE work_orders (id INTEGER PRIMARY KEY, site_id INTEGER, subject TEXT)"))
        connection.execute(text("INSERT INTO flares VALUES (1, 4, 'Battery 6 Flare'), (2, 4, 'Battery 60 Flare'), (3, 5, 'Battery 6 Flare')"))
    return DataEntryClient(engine)


def test_prepares_flare_reading_with_today_and_values():
    result = make_client().prepare(
        4,
        "flare_reading",
        "Battery 6 Flare",
        {"pressure": 10, "volume": 20},
        "2026-07-03",
    )

    assert result == {
        "status": "ready",
        "entry_type": "flare_reading",
        "entity_type": "flare",
        "entity_id": 1,
        "entity_name": "Battery 6 Flare",
        "values": {"pressure": 10, "volume": 20, "date": "2026-07-03"},
    }


def test_ambiguous_partial_name_requires_clarification():
    result = make_client().prepare(4, "flare_reading", "Battery", {}, "2026-07-03")

    assert result["status"] == "needs_clarification"
    assert [candidate["name"] for candidate in result["candidates"]] == [
        "Battery 6 Flare",
        "Battery 60 Flare",
    ]


def test_existing_reading_blocks_navigation():
    client = make_client()
    with client.engine.begin() as connection:
        connection.execute(text("INSERT INTO flare_readings VALUES (1, 1, '2026-07-03')"))

    result = client.prepare(
        4, "flare_reading", "Battery 6 Flare", {"volume": 20}, "2026-07-03"
    )

    assert result["status"] == "duplicate"


def test_unknown_fields_and_scheduled_tasks_are_rejected():
    client = make_client()

    assert client.prepare(4, "flare_reading", "Battery 6 Flare", {"bogus": 1}, "2026-07-03")["status"] == "invalid"
    assert client.prepare(4, "scheduled_task", None, {}, "2026-07-03")["status"] == "unsupported"


def test_generic_reading_asks_when_name_exists_for_multiple_entity_types():
    client = make_client()
    with client.engine.begin() as connection:
        connection.execute(text("INSERT INTO tanks VALUES (10, 4, 'Battery 6 Flare', 'linear-volume')"))

    result = client.prepare(4, "generic_reading", "Battery 6 Flare", {}, "2026-07-03")

    assert result["status"] == "needs_clarification"
    assert {candidate["type"] for candidate in result["candidates"]} == {"flare", "tank"}
