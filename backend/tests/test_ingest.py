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


from app.services.event_classification import useful_message


def test_empty_sdk_message_falls_back_to_detail():
    """Caso real issue 82613: el SDK manda '{}' y el error vive en detail."""
    detail = (
        "console.error\n[traceback] TypeError: Failed to fetch dynamically "
        "imported module: https://192.168.0.123:5175/assets/index-abc.js"
    )
    assert useful_message("{}", detail) == (
        "TypeError: Failed to fetch dynamically imported module: "
        "https://192.168.0.123:5175/assets/index-abc.js")


def test_empty_sdk_message_without_detail_is_explicit():
    assert useful_message("{}", "", "") == "(sin mensaje)"
    assert useful_message("", "console.error\n[traceback]", "") == "(sin mensaje)"


def test_real_message_is_preserved():
    assert useful_message("Unhandled TypeError while saving invoice", "x") == (
        "Unhandled TypeError while saving invoice")


def test_json_placeholders_are_treated_as_empty():
    assert useful_message("[]", "ValueError: boom") == "ValueError: boom"
    assert useful_message("null", "ValueError: boom") == "ValueError: boom"
