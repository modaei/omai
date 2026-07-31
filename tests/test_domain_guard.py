from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE, is_in_domain


def test_rejects_unrelated_question():
    assert is_in_domain("How old is Paris?") is False


def test_accepts_ometrics_workflow_question():
    assert is_in_domain("How do I register a LACT reading?") is True


def test_accepts_oilfield_report_question():
    assert is_in_domain("How much gas was flared in May?") is True


def test_accepts_data_point_trend_question():
    assert is_in_domain("analyze stroke length trend of 2510 in June") is True


def test_accepts_bare_numeric_well_status_question():
    assert is_in_domain("Why is 5248 down?") is True
    assert is_in_domain("Is 6243 shut in?") is True
    assert is_in_domain("What is the status of 4048 offline?") is True


def test_accepts_operational_lookup_for_numbered_entity():
    assert is_in_domain("What can you tell me about 4293?") is True
    assert is_in_domain("Tell me about HDU_4293") is True
    assert is_in_domain("What happened with 5144H?") is True


def test_accepts_bare_well_identifier_without_the_word_well():
    assert is_in_domain("Summarize notes about paraffin treatment for 5823.") is True
    assert is_in_domain("What was the production of 2535 last month?") is True


def test_accepts_ambiguous_bare_numeric_question_for_field_resolution():
    assert is_in_domain("How old is 5248?") is True
    assert is_in_domain("What is 5248 divided by 2?") is True


def test_accepts_follow_up_when_history_is_domain_related():
    history = [
        {
            "role": "user",
            "content": "How much gas was flared in May?",
        },
        {
            "role": "assistant",
            "content": "Use the Gas Flared report.",
        },
    ]

    assert is_in_domain("What about June?", history) is True


def test_does_not_accept_unrelated_question_just_because_history_is_domain_related():
    history = [
        {
            "role": "user",
            "content": "How much gas was flared in May?",
        }
    ]

    assert is_in_domain("How old is Paris?", history) is False
    assert OUT_OF_DOMAIN_RESPONSE == "I can only help with Ometrics related questions."
