from datetime import datetime, timezone

from omai.demo.generate_graphite import _graphite_path, _series_values


def test_graphite_path_matches_omai_trend_target_convention():
    assert _graphite_path(
        {
            "site_key": "demo",
            "facility_name": "HDU 2510",
            "tag": "peak_load_sp",
        }
    ) == "MI3.demo.HDU_2510.max_load"


def test_synthetic_series_is_deterministic_and_contains_no_negative_values():
    timestamps = [
        datetime(2026, 7, 1, hour, tzinfo=timezone.utc)
        for hour in range(3)
    ]

    first = _series_values("MI3.demo.HDU_2510.max_load", "Max Load", timestamps)
    second = _series_values("MI3.demo.HDU_2510.max_load", "Max Load", timestamps)

    assert first == second
    assert all(value >= 0 for _, value in first)
