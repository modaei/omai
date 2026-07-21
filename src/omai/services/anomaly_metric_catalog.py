from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDefinition:
    """Built-in anomaly behavior for one known metric."""

    source_type: str
    source_name: str
    metric_key: str
    comparison_mode: str
    max_percent_change: float
    max_z_score: float
    lookback_days: int = 30
    check_interval_minutes: int = 60
    flatline_minutes: int | None = None
    stale_minutes: int | None = None


class MetricCatalog:
    """Resolve default anomaly rules when users have not configured overrides."""

    def __init__(self, definitions: list[MetricDefinition] | None = None):
        self.definitions = definitions or _default_definitions()

    def for_metric(
        self,
        source_type: str,
        source_name: str,
        metric_key: str,
    ) -> MetricDefinition:
        for definition in self.definitions:
            if (
                definition.source_type == source_type
                and definition.source_name == source_name
                and definition.metric_key == metric_key
            ):
                return definition
        return MetricDefinition(
            source_type=source_type,
            source_name=source_name,
            metric_key=metric_key,
            comparison_mode="point_value",
            max_percent_change=50.0,
            max_z_score=4.0,
            stale_minutes=120 if source_type == "data_point" else None,
        )


def _default_definitions() -> list[MetricDefinition]:
    return [
        MetricDefinition("data_point", "*", "*", "point_value", 50.0, 4.0, flatline_minutes=240, stale_minutes=120),
        MetricDefinition("reading", "lact_reading", "reading", "counter_delta", 40.0, 3.5),
        MetricDefinition("reading", "flow_meter_reading", "meter_reading", "counter_delta", 40.0, 3.5),
        MetricDefinition("reading", "water_plant_reading", "meter_reading", "counter_delta", 40.0, 3.5),
        MetricDefinition("reading", "tank_reading", "top_level", "point_value", 50.0, 4.0),
        MetricDefinition("reading", "tank_reading", "water_level", "point_value", 50.0, 4.0),
        MetricDefinition("reading", "well_test", "oil", "daily_total", 40.0, 3.5),
        MetricDefinition("reading", "well_test", "water", "daily_total", 40.0, 3.5),
        MetricDefinition("reading", "well_test", "gas", "daily_total", 40.0, 3.5),
        MetricDefinition("report", "oil_production", "total_oil", "period_total", 25.0, 3.0),
        MetricDefinition("report", "oil_sale", "total_sale", "period_total", 25.0, 3.0),
        MetricDefinition("report", "gas_flared", "total_gas_flared", "period_total", 35.0, 3.5),
        MetricDefinition("report", "water_production", "total_water", "period_total", 35.0, 3.5),
        MetricDefinition("report", "water_injection", "total_injected_water", "period_total", 35.0, 3.5),
    ]
