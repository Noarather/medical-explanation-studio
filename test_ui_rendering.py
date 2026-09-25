from ui_rendering import configure_renderer


def test_windows_defaults_to_both_software_layers_without_disabling_sandbox():
    env = {}
    assert configure_renderer(env, "win32") == "software"
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu"
    assert env["QT_QUICK_BACKEND"] == "software"
    assert "--no-sandbox" not in env["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in env


def test_renderer_preserves_other_flags_and_is_idempotent():
    env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--lang=zh-CN --disable-gpu"}
    configure_renderer(env, "win32")
    first = dict(env)
    configure_renderer(env, "win32")
    assert env == first
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"] == "--lang=zh-CN --disable-gpu"


def test_auto_override_and_non_windows_do_not_change_environment():
    for env, platform in [({"MEDEXPLAIN_UI_RENDERER":"auto", "QTWEBENGINE_CHROMIUM_FLAGS":"--lang=zh-CN"}, "win32"), ({}, "linux")]:
        before = dict(env)
        assert configure_renderer(env, platform) == "auto"
        assert env == before


def test_explicit_software_and_invalid_mode_have_safe_defaults():
    env = {"MEDEXPLAIN_UI_RENDERER":"software"}
    assert configure_renderer(env, "linux") == "software"
    assert env["QT_QUICK_BACKEND"] == "software"
    assert configure_renderer({"MEDEXPLAIN_UI_RENDERER":"wrong"}, "win32") == "software"
