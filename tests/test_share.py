from __future__ import annotations

import pytest

import course_assistant.app as app
from course_assistant.config import ShareSettings, load_share_settings


def test_public_link_requires_password():
    with pytest.raises(SystemExit, match="APP_PASSWORD"):
        app.launch_options(ShareSettings(share=True, username="u", password="", read_only=True))


def test_launch_options_add_auth():
    opts = app.launch_options(ShareSettings(share=True, username="u", password="pw-123", read_only=True))
    assert opts["share"] is True and opts["auth"] == ("u", "pw-123")
    local = app.launch_options(ShareSettings(share=False, username="u", password="", read_only=False))
    assert local["share"] is False and "auth" not in local


def test_password_not_in_repr():
    assert "pw-123" not in repr(ShareSettings(share=True, username="u", password="pw-123", read_only=True))


def test_read_only_defaults_on_when_shared(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    for k in ("SHARE", "READ_ONLY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SHARE", "true")
    assert load_share_settings(env).read_only is True
    monkeypatch.setenv("READ_ONLY", "false")
    assert load_share_settings(env).read_only is False


def _button_labels(demo):
    return {b.value for b in demo.blocks.values() if b.__class__.__name__ == "Button"}


def test_read_only_ui_has_no_upload_or_remove(monkeypatch, lib):
    monkeypatch.setattr(app, "get_library", lambda: lib)
    assert {"Remove", "Add to library"} <= _button_labels(app.build_ui(read_only=False))
    labels = _button_labels(app.build_ui(read_only=True))
    assert "Remove" not in labels and "Add to library" not in labels
    assert {"Ask", "Generate quiz"} <= labels


def test_server_host_counts_as_public_and_needs_password():
    s = ShareSettings(share=False, username="u", password="", read_only=True, host="0.0.0.0", port=8080)
    assert s.public
    with pytest.raises(SystemExit):
        app.launch_options(s)
    ok = app.launch_options(ShareSettings(share=False, username="u", password="pw", read_only=True,
                                          host="0.0.0.0", port=8080))
    assert ok["server_name"] == "0.0.0.0" and ok["server_port"] == 8080 and ok["share"] is False


def test_host_and_port_from_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    for k in ("SHARE", "READ_ONLY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9999")
    s = load_share_settings(env)
    assert (s.host, s.port, s.read_only) == ("0.0.0.0", 9999, True)


def test_login_can_be_turned_off_explicitly(monkeypatch, tmp_path):
    no_login = ShareSettings(share=False, username="u", password="pw", read_only=True,
                             host="0.0.0.0", port=8080, require_login=False)
    opts = app.launch_options(no_login)
    assert "auth" not in opts and opts["server_name"] == "0.0.0.0"
    # without a password it still starts, because the login was deliberately turned off
    app.launch_options(ShareSettings(share=False, username="u", password="", read_only=True,
                                     host="0.0.0.0", port=8080, require_login=False))
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    assert load_share_settings(env).require_login is False
    monkeypatch.delenv("REQUIRE_LOGIN")
    assert load_share_settings(env).require_login is True  # login stays on by default


def test_local_run_has_no_login_even_with_password():
    local = ShareSettings(share=False, username="u", password="pw", read_only=False)
    assert "auth" not in app.launch_options(local)


def test_library_password_guards_add_and_remove(monkeypatch, lib):
    import gradio as gr
    import pytest as _pytest

    monkeypatch.setattr(app, "get_library", lambda: lib)
    demo = app.build_ui(read_only=False, edit_password="lib-pw")
    assert any(getattr(b, "label", "") and "Library password" in b.label for b in demo.blocks.values())
    doc = next(iter(lib.docs))
    app.remove_doc(doc, "wrong")  # refused with a warning, nothing removed
    assert doc in lib.docs
    assert "Wrong library password" in app.add_files([], "wrong")[0]
    app.remove_doc(doc, "lib-pw")
    assert doc not in lib.docs
    app.build_ui(read_only=False, edit_password=None)  # local: no password needed
    app.remove_doc(next(iter(lib.docs)), None)


def test_library_editing_needs_no_password_by_default(monkeypatch, lib):
    monkeypatch.setattr(app, "get_library", lambda: lib)
    demo = app.build_ui(read_only=False, edit_password=None)
    assert not any("Library password" in (getattr(b, "label", "") or "") for b in demo.blocks.values())
    doc = next(iter(lib.docs))
    app.remove_doc(doc, None)
    assert doc not in lib.docs
