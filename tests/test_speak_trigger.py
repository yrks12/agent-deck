"""When the manager is allowed to speak, and when it must stay quiet.

The whole point is that you never type /speak again. The risk that buys is a
daemon that talks at you: over a turn you did not start, or twice for the same
reply. Both are gated here.
"""

from server import manager


def speaker():
    spoken = []
    return manager.ReplySpeaker(runner=spoken.append), spoken


def test_speaks_the_reply_to_a_message_we_sent():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    out = sp.check("sid-m", done_at=110.0, text="He deployed at 8:44.", message_id="m1")
    assert out == "He deployed at 8:44."
    assert spoken == ["He deployed at 8:44."]


def test_never_speaks_a_turn_we_did_not_start():
    sp, spoken = speaker()
    assert sp.check("sid-m", done_at=110.0, text="unrelated work", message_id="m1") is None
    assert spoken == []


def test_never_speaks_a_turn_that_ended_before_we_asked():
    sp, spoken = speaker()
    sp.arm("sid-m", now=200.0)
    assert sp.check("sid-m", done_at=110.0, text="stale reply", message_id="m1") is None
    assert spoken == []


def test_never_speaks_the_same_message_twice():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    sp.check("sid-m", done_at=110.0, text="first", message_id="m1")
    sp.check("sid-m", done_at=110.0, text="first", message_id="m1")
    assert spoken == ["first"]


def test_one_message_sent_buys_exactly_one_reply_spoken():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    sp.check("sid-m", done_at=110.0, text="first", message_id="m1")
    sp.check("sid-m", done_at=120.0, text="second", message_id="m2")
    assert spoken == ["first"], "the second turn was not ours to speak"


def test_muted_arms_without_speaking():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0, muted=True)
    assert sp.check("sid-m", done_at=110.0, text="quiet", message_id="m1") is None
    assert spoken == []


def test_a_replayed_reply_is_not_spoken_again_after_a_second_message():
    """The disarm alone is not enough.

    Send, hear the reply, send again -- and the transcript tail re-offers the
    SAME message id because nothing new has landed yet. Without the spoken-once
    set the daemon repeats the old answer as if it were the new one.
    """
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    sp.check("sid-m", done_at=110.0, text="the first answer", message_id="m1")
    sp.arm("sid-m", now=120.0)
    assert sp.check("sid-m", done_at=130.0, text="the first answer", message_id="m1") is None
    assert spoken == ["the first answer"]


def test_arming_again_re_enables_the_next_reply():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    sp.check("sid-m", done_at=110.0, text="first", message_id="m1")
    sp.arm("sid-m", now=200.0)
    sp.check("sid-m", done_at=210.0, text="second", message_id="m2")
    assert spoken == ["first", "second"]


def test_another_session_s_turn_is_never_spoken():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    assert sp.check("sid-other", done_at=110.0, text="not yours", message_id="m9") is None
    assert spoken == []


def test_an_empty_or_idless_reply_is_not_spoken():
    sp, spoken = speaker()
    sp.arm("sid-m", now=100.0)
    assert sp.check("sid-m", done_at=110.0, text="", message_id="m1") is None
    assert sp.check("sid-m", done_at=110.0, text="hi", message_id="") is None
    assert spoken == []


def test_strip_for_speech_drops_code_and_markdown():
    text = "Merged **#372**.\n\n```bash\ngit merge develop\n```\n\n- one\n- two"
    out = manager.strip_for_speech(text)
    assert "```" not in out
    assert "git merge" not in out
    assert "**" not in out
    assert "Merged #372." in out


def test_strip_for_speech_clips_long_text():
    assert len(manager.strip_for_speech("word " * 1000)) <= manager.SPEECH_LIMIT


def test_an_existing_mlx_tts_install_is_the_speak_cmd(tmp_path):
    """A home with the mlx-tts pipeline keeps it, so ⌥P / ⌥R / ⌥S keep working."""
    base = tmp_path / "Applications" / "mlx-tts"
    base.mkdir(parents=True)
    (base / "speak.py").write_text("")
    cmd = manager.build_speak_cmd({}, home=tmp_path)
    assert cmd[:2] == [str(base / "venv" / "bin" / "python3"), str(base / "speak.py")]
    assert "--background" in cmd and "--speed" in cmd and "1.1" in cmd


def test_without_it_a_mac_uses_say_and_anything_else_is_silent(tmp_path):
    assert manager.build_speak_cmd({}, home=tmp_path, which=lambda _: "/usr/bin/say") == ["say", "{text}"]
    assert manager.build_speak_cmd({}, home=tmp_path, which=lambda _: None) == []
