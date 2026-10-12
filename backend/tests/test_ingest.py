from app.services.event_classification import normalize_reported_level


def test_browser_view_transition_aborts_are_warnings():
    assert normalize_reported_level(
        "ERROR", "Transition was aborted because of invalid state. Document hidden"
    ) == "WARNING"
    assert normalize_reported_level(
        "ERROR", "Old view transition aborted by new view transition."
    ) == "WARNING"


def test_wpp_session_disconnect_is_warning():
    message = "[WPPCONNECT] Session Disconnected while sending message"
    assert normalize_reported_level("ERROR", message) == "WARNING"


def test_unexpected_application_error_remains_error():
    assert normalize_reported_level(
        "error", "Unhandled TypeError while saving invoice"
    ) == "ERROR"
