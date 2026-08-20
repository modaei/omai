import pytest

from omai.demo.sanitize_database import (
    DatabaseSanitizer,
    DemoDumpSanitizer,
    SanitizationError,
    SanitizationOptions,
    _is_measurement_column,
    _measurement_domain,
    _safe_data_point_name,
    _slug,
)


def test_measurement_selection_excludes_relationships_and_status_fields():
    assert _is_measurement_column("oil_volume") is True
    assert _is_measurement_column("bbl_foot") is True
    assert _is_measurement_column("well_id") is False
    assert _is_measurement_column("unix_timestamp") is False
    assert _is_measurement_column("active") is False


def test_measurement_domains_keep_related_oil_values_on_one_factor():
    assert _measurement_domain("oil_volume") == "oil"
    assert _measurement_domain("oil_sale") == "oil"
    assert _measurement_domain("motor_temperature") == "temperature"


def test_sanitized_data_point_names_and_tags_are_safe_and_usable():
    assert _safe_data_point_name("Pump Fillage", 11) == "Pump Fillage"
    assert _safe_data_point_name(None, 11) == "Measurement 11"
    assert _slug("Well 101 / Pump Fillage") == "well_101_pump_fillage"


def test_entity_aliases_are_deterministic_and_unique_within_one_entity_type():
    sanitizer = DatabaseSanitizer.__new__(DatabaseSanitizer)
    sanitizer.seed = b"test-seed"

    aliases = sanitizer._unique_entity_aliases("Well", [437, 12, 900, 12])

    assert aliases == sanitizer._unique_entity_aliases("Well", [437, 12, 900])
    assert len(aliases) == 3
    assert len(set(aliases.values())) == 3
    assert all(alias.startswith("Well ") for alias in aliases.values())


def test_database_selecting_source_dump_is_rejected_before_import(tmp_path):
    source = tmp_path / "unsafe.sql"
    source.write_text("CREATE DATABASE ometrics;\nUSE ometrics;\n", encoding="utf-8")
    sanitizer = DemoDumpSanitizer(
        SanitizationOptions(
            input_path=source,
            site_id=4,
            demo_user_id=1,
            seed=b"test-seed",
            staging_database="omai_demo_staging",
            database_host="127.0.0.1",
            database_port=3306,
            database_user="root",
            database_password="password",
            target_database="ometrics_demo",
            rollback_directory=tmp_path / "rollback",
        )
    )

    with pytest.raises(SanitizationError, match="single-database dump"):
        sanitizer._validate_dump_format()
