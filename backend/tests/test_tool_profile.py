import coach_chat


def test_voice_profile_excludes_fire_intake():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("voice")}
    assert "fire_intake_trigger" not in names
    assert "swap_exercise" in names


def test_text_profile_includes_everything():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("text")}
    assert "fire_intake_trigger" in names


def test_unknown_profile_defaults_to_voice_safe():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("???")}
    assert "fire_intake_trigger" not in names
