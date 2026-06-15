import pytest
from sqlalchemy import create_engine, text

from omai.clients.reading_client import (
    ReadingClient,
    ReadingClientError,
    UnavailableReadingClient,
)


def make_sqlite_client() -> ReadingClient:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE lacts (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    disable_reading INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE lact_readings (
                    id INTEGER PRIMARY KEY,
                    lact_id INTEGER NOT NULL,
                    reading REAL,
                    temperature REAL,
                    bs_w REAL,
                    comments TEXT,
                    time TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE flares (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    disable_reading INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE flare_readings (
                    id INTEGER PRIMARY KEY,
                    flare_id INTEGER NOT NULL,
                    pressure REAL,
                    volume REAL,
                    comments TEXT,
                    time TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO lacts (id, site_id, name, disable_reading)
                VALUES
                    (1, 1, 'LACT A', 0),
                    (2, 1, 'LACT B', 0),
                    (3, 1, 'LACT Disabled', 1),
                    (4, 2, 'Other Site LACT', 0)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO flares (id, site_id, name, disable_reading)
                VALUES
                    (1, 1, 'Flare A', 0),
                    (2, 1, 'Flare B', 1),
                    (3, 2, 'Other Site Flare', 0)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO lact_readings
                    (id, lact_id, reading, temperature, bs_w, comments, time)
                VALUES
                    (10, 1, 123.4, 88.0, 0.1, 'normal', '2026-06-10 08:00:00'),
                    (11, 1, 140.0, 90.0, 0.1, 'next day', '2026-06-11 08:00:00'),
                    (12, 4, 999.0, 90.0, 0.1, 'other site', '2026-06-10 08:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO flare_readings
                    (id, flare_id, pressure, volume, comments, time)
                VALUES
                    (20, 3, 10.0, 100.0, 'other site', '2026-06-10 08:00:00')
                """
            )
        )
    return ReadingClient(engine)


def make_mixed_tank_client() -> ReadingClient:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE tanks (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    disable_reading INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE mixed_tank_readings (
                    id INTEGER PRIMARY KEY,
                    tank_id INTEGER NOT NULL,
                    top_level_feet INTEGER,
                    top_level_inches REAL,
                    water_level_feet INTEGER,
                    water_level_inches REAL,
                    comments TEXT,
                    time TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tanks (id, site_id, name, type, disable_reading)
                VALUES (1, 1, '2-1 Float Over', 'mixed-water-oil', 0)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO mixed_tank_readings
                    (
                        id,
                        tank_id,
                        top_level_feet,
                        top_level_inches,
                        water_level_feet,
                        water_level_inches,
                        comments,
                        time
                    )
                VALUES
                    (10, 1, 8, 0, 1, 2, 'first', '2026-05-09 08:00:00'),
                    (11, 1, 4, 1, 1, 2, 'second', '2026-05-10 08:00:00')
                """
            )
        )
    return ReadingClient(engine)


def make_well_test_client() -> ReadingClient:
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
                    oil REAL,
                    water REAL,
                    gas REAL,
                    pip REAL,
                    m_temp REAL,
                    amps REAL,
                    tbgp REAL,
                    csgp REAL,
                    runtime REAL,
                    comments TEXT,
                    time TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO wells (id, site_id, name)
                VALUES
                    (1, 1, 'HARTZOG DRAW UNIT 4048'),
                    (2, 1, 'HARTZOG DRAW UNIT 4050'),
                    (3, 2, 'OTHER SITE WELL')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO well_tests
                    (
                        id,
                        well_id,
                        oil,
                        water,
                        gas,
                        pip,
                        m_temp,
                        amps,
                        tbgp,
                        csgp,
                        runtime,
                        comments,
                        time
                    )
                VALUES
                    (10, 1, 25.5, 12.0, 30.0, NULL, NULL, NULL, NULL, NULL, 24.0, 'good', '2026-05-01 08:00:00'),
                    (11, 2, 19.5, 9.0, 20.0, NULL, NULL, NULL, NULL, NULL, 24.0, 'low oil', '2026-05-02 08:00:00'),
                    (12, 1, 40.0, 15.0, 35.0, NULL, NULL, NULL, NULL, NULL, 20.0, 'strong', '2026-06-05 08:00:00'),
                    (13, 1, 50.0, 20.0, 45.0, NULL, NULL, NULL, NULL, NULL, 20.0, 'outside range', '2026-06-06 08:00:00'),
                    (14, 3, 90.0, 1.0, 1.0, NULL, NULL, NULL, NULL, NULL, 24.0, 'other site', '2026-05-01 08:00:00')
                """
            )
        )
    return ReadingClient(engine)


def test_get_readings_for_date_filters_by_site_and_day():
    result = make_sqlite_client().get_readings_for_date(1, "lact", "2026-06-10")

    assert result["count"] == 1
    assert result["readings"][0] == {
        "entity_display_name": "LACT - LACT A",
        "entity_type": "LACT",
        "reading": 123.4,
        "temperature": 88.0,
        "bs_w": 0.1,
        "comments": "normal",
    }
    assert "reading_id" not in result["readings"][0]
    assert "time" not in result["readings"][0]


def test_compare_readings_between_dates_returns_entity_summary():
    result = make_sqlite_client().compare_readings_between_dates(
        1, "lact", "2026-06-10", "2026-06-11"
    )

    assert result["first_count"] == 1
    assert result["second_count"] == 1
    assert "first_readings" not in result
    assert "second_readings" not in result
    assert result["entity_summary"] == [
        {
            "entity_display_name": "LACT - LACT A",
            "first_count": 1,
            "second_count": 1,
            "status": "present_on_both_dates",
        }
    ]
    assert result["comparison_rows"][0]["entity_display_name"] == "LACT - LACT A"


def test_compare_mixed_tank_readings_returns_compact_changes():
    result = make_mixed_tank_client().compare_readings_between_dates(
        1, "mixed_tank", "2026-05-09", "2026-05-10"
    )

    assert "first_readings" not in result
    assert "second_readings" not in result
    assert result["comparison_rows"] == [
        {
            "entity_display_name": "Tank - 2-1 Float Over",
            "status": "compared",
            "changes": [
                {
                    "field": "top_level",
                    "label": "Top level",
                    "first_value": "8'0\"",
                    "second_value": "4'1\"",
                    "direction": "decrease",
                    "delta_inches": -47.0,
                    "delta": "3'11\"",
                },
            ],
        }
    ]


def test_missing_readings_excludes_disabled_and_other_sites():
    result = make_sqlite_client().missing_readings_for_date(1, "lact", "2026-06-10")

    assert result["missing_count"] == 1
    assert result["missing_entities"] == [
        {
            "entity_name": "LACT B",
            "entity_type": "LACT",
            "entity_display_name": "LACT - LACT B",
        }
    ]


def test_all_missing_readings_checks_all_supported_types_with_tables():
    result = make_sqlite_client().all_missing_readings_for_date(1, "2026-06-10")

    assert result["missing_count"] == 2
    assert [group["reading_type"] for group in result["groups"]] == ["lact", "flare"]
    assert result["groups"][0]["missing_entities"] == [
        {
            "entity_name": "LACT B",
            "entity_type": "LACT",
            "entity_display_name": "LACT - LACT B",
        }
    ]
    assert result["groups"][1]["missing_entities"] == [
        {
            "entity_name": "Flare A",
            "entity_type": "Flare",
            "entity_display_name": "Flare - Flare A",
        }
    ]


def test_search_well_tests_filters_date_range_site_and_oil_threshold():
    result = make_well_test_client().search_well_tests(
        1,
        "2026-05-01",
        "2026-06-05",
        min_oil=20,
    )

    assert result["count"] == 2
    assert result["filters"] == {"min_oil": 20}
    assert result["well_tests"] == [
        {
            "date": "2026-05-01",
            "entity_display_name": "Well - HARTZOG DRAW UNIT 4048",
            "entity_type": "Well",
            "oil": 25.5,
            "water": 12.0,
            "gas": 30.0,
            "runtime": 24.0,
            "comments": "good",
        },
        {
            "date": "2026-06-05",
            "entity_display_name": "Well - HARTZOG DRAW UNIT 4048",
            "entity_type": "Well",
            "oil": 40.0,
            "water": 15.0,
            "gas": 35.0,
            "runtime": 20.0,
            "comments": "strong",
        },
    ]


def test_search_readings_filters_lact_range_and_numeric_field():
    result = make_sqlite_client().search_readings(
        1,
        "lact",
        "2026-06-10",
        "2026-06-11",
        filters=[{"field": "reading", "operator": ">", "value": 130}],
    )

    assert result["count"] == 1
    assert result["filters"] == {
        "field_filters": [{"field": "reading", "operator": ">", "value": 130}]
    }
    assert result["readings"] == [
        {
            "date": "2026-06-11",
            "entity_display_name": "LACT - LACT A",
            "entity_type": "LACT",
            "reading": 140.0,
            "temperature": 90.0,
            "bs_w": 0.1,
            "comments": "next day",
        }
    ]


def test_search_readings_rejects_unsupported_filter_field():
    with pytest.raises(ReadingClientError, match="unsupported field"):
        make_sqlite_client().search_readings(
            1,
            "lact",
            "2026-06-10",
            "2026-06-11",
            filters=[{"field": "comments", "operator": "=", "value": 1}],
        )


def test_search_well_tests_rejects_reversed_date_range():
    with pytest.raises(ReadingClientError, match="end_date must be"):
        make_well_test_client().search_well_tests(
            1,
            "2026-06-05",
            "2026-05-01",
        )


def test_unknown_reading_type_is_rejected():
    with pytest.raises(ReadingClientError, match="Unsupported reading type"):
        make_sqlite_client().get_readings_for_date(1, "unknown", "2026-06-10")


def test_unavailable_client_lists_types_but_rejects_queries():
    client = UnavailableReadingClient("DB settings missing")

    assert any(item["reading_type"] == "lact" for item in client.list_reading_types())
    with pytest.raises(ReadingClientError, match="DB settings missing"):
        client.get_readings_for_date(1, "lact", "2026-06-10")
