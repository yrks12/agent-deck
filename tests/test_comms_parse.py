from server.sources import comms

INBOUND = (
    '<cross-session-message from="uds:/tmp/cc-socks/67127.sock" '
    'from-name="Root-cause audit fixes" from-mode="bypass">\n'
    "Both rulings applied and on record.\n"
    "</cross-session-message>"
)


def test_parse_inbound_pulls_sender_socket_name_and_body():
    got = comms.parse_inbound(INBOUND)
    assert got["from_sock"] == "uds:/tmp/cc-socks/67127.sock"
    assert got["from_name"] == "Root-cause audit fixes"
    assert got["from_mode"] == "bypass"
    assert got["body"].startswith("Both rulings applied")
    assert "</cross-session-message>" not in got["body"]


def test_parse_inbound_ignores_ordinary_text():
    assert comms.parse_inbound("where is he at? /speak") is None
    assert comms.parse_inbound("") is None


def test_parse_inbound_survives_a_missing_mode():
    text = (
        '<cross-session-message from="uds:/tmp/cc-socks/9.sock" from-name="x">'
        "hi</cross-session-message>"
    )
    got = comms.parse_inbound(text)
    assert got["from_mode"] == ""
    assert got["body"] == "hi"


def test_pid_from_sock():
    assert comms.pid_from_sock("uds:/tmp/cc-socks/67127.sock") == 67127
    assert comms.pid_from_sock("uds:/tmp/cc-socks-501/8.sock") == 8
    assert comms.pid_from_sock("orion-fd") is None


def test_parse_target_splits_name_from_ref():
    assert comms.parse_target("orion-fd [156688]") == ("orion-fd", "156688")
    assert comms.parse_target("orion-fd") == ("orion-fd", "")
    assert comms.parse_target("uds:/tmp/cc-socks/31705.sock") == (
        "uds:/tmp/cc-socks/31705.sock",
        "",
    )


def test_message_key_is_stable_under_whitespace_and_truncation():
    a = comms.message_key("Owner ruling relayed  from the main session")
    b = comms.message_key("Owner ruling relayed from the main session")
    assert a == b
    long_a = comms.message_key("x" * 200 + "AAA")
    long_b = comms.message_key("x" * 200 + "BBB")
    assert long_a == long_b, "keys compare only the first 200 chars"
