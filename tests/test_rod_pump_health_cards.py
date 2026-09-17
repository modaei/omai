from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from omai.services.rod_pump_health import _card_feature_row, _meaningful_transition_baseline, _select_current_card_medoid, _stable_pumping_context, _within_paraffin_event_window


def _card(card_id: int, loads: list[float], orientation: str | None = "upstroke_first") -> dict:
    return {
        "source_card_id": card_id,
        "data_points": [{"position": index, "load": load} for index, load in enumerate(loads)],
        "info": {} if orientation is None else {"orientation": orientation},
    }


def test_current_card_medoid_selects_a_real_middle_curve() -> None:
    selected = _select_current_card_medoid([
        _card(3, [0, 0, 0, 0]),
        _card(2, [0, 1, 2, 3]),
        _card(1, [0, 2, 4, 6]),
    ])

    assert selected is not None
    assert selected["source_card_id"] == 1  # normalized curves tie; lowest real card ID wins.
    assert selected["selection_metadata"]["algorithm"] == "current-card-medoid-v1"


def test_unknown_orientation_is_persisted_but_not_valid_for_diagnosis() -> None:
    row = _card_feature_row(_card(7, list(range(20)), None), 10, 1_700_000_000, "surface", ZoneInfo("UTC"))

    assert row["quality_status"] == "orientation_unknown"
    assert row["orientation"] is None
    assert "position_traversal_is_not_one_verified_stroke" in row["quality_reasons_json"]


def test_verified_orientation_creates_valid_feature_row() -> None:
    row = _card_feature_row(_card(7, list(range(20))), 10, 1_700_000_000, "downhole", ZoneInfo("UTC"))

    assert row["quality_status"] == "valid"
    assert row["orientation"] == "verified_upstroke_first"


def test_position_traversal_derives_a_verified_orientation_without_metadata() -> None:
    positions = list(range(11)) + list(range(9, -1, -1))
    source = {
        "source_card_id": 8,
        "data_points": [{"position": position, "load": index} for index, position in enumerate(positions)],
        "info": {},
    }

    row = _card_feature_row(source, 10, 1_700_000_000, "surface", ZoneInfo("UTC"))

    assert row["quality_status"] == "valid"
    assert row["orientation"] == "verified_upstroke_first"


def test_stable_pumping_context_requires_15_minutes_before_and_10_after() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    samples = [{"time": card_time + timedelta(minutes=offset), "code": 7} for offset in (-30, -15, 0, 15)]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is True
    assert context["window_before_minutes"] == 15
    assert context["window_after_minutes"] == 10


def test_stable_pumping_context_rejects_a_transition_to_downtime_inside_window() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    samples = [{"time": card_time + timedelta(minutes=offset), "code": 7} for offset in (-30, -15, 0, 15)]
    samples[2]["code"] = 31

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is False
    assert context["reason"] == "well_state_code_transition_uncertain"


def test_stable_pumping_context_accepts_a_late_observed_pumping_confirmation() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    # The first sample after the required +10-minute endpoint arrives at +27.
    # It still confirms pumping under the agreed sampled-state convention.
    samples = [{"time": card_time + timedelta(minutes=offset), "code": 7} for offset in (-30, -15, 0, 27)]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is True
    assert context["confirmation_at"] == (card_time + timedelta(minutes=27)).isoformat()


def test_stable_pumping_context_accepts_a_sparse_surrounding_pumping_run() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    # Graphite can return state observations an hour apart. With no observed
    # non-pumping state, the run still establishes the required window.
    samples = [{"time": card_time + timedelta(minutes=offset), "code": 7} for offset in (-68, -2, 25)]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is True


def test_stable_pumping_context_carries_a_repeated_state_across_any_gap() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    # The two observed 7s bracket the whole required window.  The gap is
    # deliberately much longer than a normal Graphite sampling interval.
    samples = [
        {"time": card_time - timedelta(days=4), "code": 7},
        {"time": card_time + timedelta(days=3), "code": 7},
    ]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is True
    assert context["state_at_card"] == 7


def test_stable_pumping_context_rejects_a_state_change_bracketing_the_card() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    samples = [
        {"time": card_time - timedelta(hours=1), "code": 7},
        {"time": card_time + timedelta(hours=1), "code": 31},
    ]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is False
    assert context["reason"] == "well_state_code_transition_uncertain"
    assert context["previous_state_code"] == 7
    assert context["next_state_code"] == 31


def test_stable_pumping_context_rejects_a_change_between_pumping_modes() -> None:
    card_time = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    samples = [
        {"time": card_time - timedelta(hours=1), "code": 7},
        {"time": card_time + timedelta(hours=1), "code": 8},
    ]

    context = _stable_pumping_context(card_time, samples)

    assert context["eligible"] is False
    assert context["reason"] == "well_state_code_transition_uncertain"


def test_paraffin_event_excludes_cards_three_days_before_through_after() -> None:
    event = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))

    assert _within_paraffin_event_window(event - timedelta(days=3), event) is True
    assert _within_paraffin_event_window(event + timedelta(days=3), event) is True
    assert _within_paraffin_event_window(event + timedelta(days=3, seconds=1), event) is False


def test_valve_rules_reject_a_zero_width_clean_transition_reference() -> None:
    assert _meaningful_transition_baseline({'bottom_transition_width': {'median': 0.0}}, 'bottom_transition_width') is False
    assert _meaningful_transition_baseline({'bottom_transition_width': {'median': 0.25}}, 'bottom_transition_width') is True
