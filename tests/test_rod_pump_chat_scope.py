from omai.services.chat_service import is_rod_pump_fleet_question


def test_blocks_expensive_rod_pump_fleet_questions():
    assert is_rod_pump_fleet_question("Rank all rod-pump wells by current health")
    assert is_rod_pump_fleet_question("Which wells have the highest mechanical risk?")
    assert is_rod_pump_fleet_question("Which pump wells need urgent intervention?")


def test_allows_one_exact_well_question():
    assert not is_rod_pump_fleet_question("Analyze rod-pump health for well 5823")
