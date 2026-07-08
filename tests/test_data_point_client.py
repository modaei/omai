import json

from sqlalchemy import create_engine, text

from omai.clients.data_point_client import DataPointClient


def make_client() -> DataPointClient:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE data_points (
                    id INTEGER PRIMARY KEY,
                    site_id INTEGER NOT NULL,
                    facility_id INTEGER,
                    facility_name TEXT,
                    device_name TEXT,
                    data_point_name TEXT,
                    type TEXT,
                    active INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE data_point_data (
                    id INTEGER PRIMARY KEY,
                    data_point_id INTEGER UNIQUE,
                    data TEXT,
                    last_update INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO data_points
                    (id, site_id, facility_id, facility_name, device_name, data_point_name, type, active)
                VALUES
                    (1, 1, NULL, 'HDU_2510', NULL, NULL, 'facility', 1),
                    (2, 1, 1, NULL, 'HDU_2510', 'stroke_min', 'number', 1),
                    (3, 1, 1, NULL, 'HDU_2510', 'min_load_ls', 'number', 1),
                    (4, 1, 1, NULL, 'HDU_2510', 'min_load_sp', 'number', 0),
                    (5, 1, 1, NULL, 'HDU_2510', 'min_load_consecutive_sp', 'number', 1),
                    (6, 1, 1, NULL, 'HDU_2510', 'yesterday_min_load', 'number', 1),
                    (7, 1, 1, NULL, 'Controller A', 'pressure', 'number', 1),
                    (8, 2, NULL, 'HDU_2510', 'HDU_2510', 'stroke_min', 'number', 1),
                    (9, 1, 1, NULL, 'HDU_2510', 'bad_payload', 'number', 1),
                    (10, 1, NULL, 'ABC_9999', 'ABC_9999', 'value', 'number', 1),
                    (11, 1, NULL, 'XYZ_9999', 'XYZ_9999', 'value', 'number', 1)
                """
            )
        )
        rows = [
            (2, {"value": 4.84, "type": "Real", "timestamp": 1_717_241_400}),
            (3, {"value": 15449, "type": "Real", "timestamp": 1_717_241_400}),
            (4, {"value": 13800, "type": "Real", "timestamp": 1_717_241_400}),
            (5, {"value": 5, "type": "Real", "timestamp": 1_717_241_400}),
            (6, {"value": 15297, "type": "Real", "timestamp": 1_717_241_400}),
            (7, {"value": 75, "type": "Real"}),
            (8, {"value": 999, "type": "Real", "timestamp": 1_717_241_400}),
        ]
        for index, (data_point_id, payload) in enumerate(rows, start=1):
            connection.execute(
                text(
                    "INSERT INTO data_point_data (id, data_point_id, data, last_update) "
                    "VALUES (:id, :data_point_id, :data, :last_update)"
                ),
                {
                    "id": index,
                    "data_point_id": data_point_id,
                    "data": json.dumps(payload),
                    "last_update": 1_717_241_500,
                },
            )
        connection.execute(
            text(
                "INSERT INTO data_point_data (id, data_point_id, data, last_update) "
                "VALUES (20, 9, 'not-json', 1717241500)"
            )
        )
    return DataPointClient(engine, "UTC")


def test_resolves_facility_shorthand_and_exact_normalized_data_point():
    result = make_client().get_values(1, "2510", "stroke min")

    assert result["status"] == "found"
    assert result["facility_name"] == "HDU_2510"
    assert result["device_name"] == "HDU_2510"
    assert result["device_defaulted"] is True
    assert result["values"][0]["data_point_name"] == "stroke_min"
    assert result["values"][0]["value"] == 4.84
    assert result["values"][0]["received_at"].endswith("UTC")


def test_returns_only_closest_prefix_data_point_matches_and_includes_inactive():
    result = make_client().get_values(1, "HDU 2510", "min load")

    assert [item["data_point_name"] for item in result["values"]] == [
        "min_load_ls",
        "min_load_sp",
    ]
    assert result["values"][1]["active"] is False


def test_explicit_device_is_matched_with_normalization():
    result = make_client().get_values(
        1, "HDU-2510", "pressure", device_name="Controller-A"
    )

    assert result["status"] == "found"
    assert result["device_defaulted"] is False
    assert result["device_name"] == "Controller A"
    assert result["values"][0]["value"] == 75
    assert result["values"][0]["received_at"].endswith("UTC")


def test_facility_ambiguity_returns_candidates_without_values():
    result = make_client().get_values(1, "9999", "value")

    assert result["status"] == "ambiguous"
    assert result["selector"] == "facility"
    assert result["candidates"] == ["ABC_9999", "XYZ_9999"]
    assert "values" not in result


def test_malformed_payload_is_reported_without_crashing():
    result = make_client().get_values(1, "2510", "bad payload")

    value = result["values"][0]
    assert value["value_available"] is False
    assert value["received_at"].endswith("UTC")
    assert value["data_error"] == "Current value payload is invalid JSON."


def test_lookup_is_strictly_site_scoped():
    result = make_client().get_values(1, "2510", "stroke min")

    assert result["values"][0]["value"] == 4.84
