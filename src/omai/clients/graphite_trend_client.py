from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx


class GraphiteTrendClientError(RuntimeError):
    """Raised when Graphite trends cannot be fetched."""


@dataclass(frozen=True)
class GraphiteTarget:
    """One Graphite target and the Ometrics data point it represents."""

    data_point_id: int
    target: str


class GraphiteTrendClient:
    """Fetch many Graphite series in bounded batches instead of one call per target."""

    def __init__(
        self,
        api_url: str,
        timezone_name: str,
        timeout_seconds: float = 30,
        batch_size: int = 100,
    ):
        self.api_url = api_url
        self.timezone = ZoneInfo(timezone_name)
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size

    def fetch(
        self,
        targets: list[GraphiteTarget],
        start: datetime,
        end: datetime,
        max_data_points: int = 720,
    ) -> dict[int, list[dict[str, Any]]]:
        """Return timestamp/value points keyed by Ometrics data_point id."""
        results: dict[int, list[dict[str, Any]]] = {}
        for batch in _chunks(targets, self.batch_size):
            results.update(self._fetch_batch(batch, start, end, max_data_points))
        return results

    def _fetch_batch(
        self,
        targets: list[GraphiteTarget],
        start: datetime,
        end: datetime,
        max_data_points: int,
    ) -> dict[int, list[dict[str, Any]]]:
        target_by_name = {target.target: target for target in targets}
        params = [("target", target.target) for target in targets]
        params.extend(
            [
                ("from", start.strftime("%H:%M_%Y%m%d")),
                ("until", end.strftime("%H:%M_%Y%m%d")),
                ("format", "json"),
                ("noNullPoints", "true"),
                ("maxDataPoints", str(max_data_points)),
                ("tz", str(self.timezone)),
            ]
        )
        try:
            response = httpx.post(
                self.api_url,
                data=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if len(targets) > 25:
                split_size = max(25, len(targets) // 2)
                nested = GraphiteTrendClient(
                    self.api_url,
                    str(self.timezone),
                    self.timeout_seconds,
                    split_size,
                )
                return nested.fetch(targets, start, end, max_data_points)
            raise GraphiteTrendClientError(f"Graphite request failed: {exc}") from exc
        if not isinstance(payload, list):
            raise GraphiteTrendClientError("Graphite response must be a list.")
        return {
            target_by_name[str(item.get("target"))].data_point_id: _points(item, self.timezone)
            for item in payload
            if isinstance(item, dict) and str(item.get("target")) in target_by_name
        }


def _points(item: dict[str, Any], timezone: ZoneInfo) -> list[dict[str, Any]]:
    points = []
    for raw in item.get("datapoints") or []:
        if len(raw) < 2 or raw[0] is None or raw[1] is None:
            continue
        points.append(
            {
                "time": datetime.fromtimestamp(float(raw[1]), timezone),
                "value": float(raw[0]),
            }
        )
    return points


def _chunks(values: list[GraphiteTarget], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]
