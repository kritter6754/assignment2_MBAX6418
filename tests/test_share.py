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
