"""K3 rate limiting: who the client is, when it is banned, what the journal says.

In-process and memory-only by design (a restart clears bans -- documented in
the plan). The journal line is the fail2ban hook, so its shape is pinned and
it must never carry a presented token.
"""

import logging

import pytest

from server import ratelimit
from server.ratelimit import RateLimiter, client_ip, log_failure


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("peer, xff, mode, want", [
    ("198.51.100.4", "203.0.113.9", "public", "198.51.100.4"),    # only loopback is trusted
    ("127.0.0.1", "203.0.113.9", "public", "203.0.113.9"),        # Caddy on the box
    ("127.0.0.1", "10.0.0.1, 203.0.113.9", "public", "203.0.113.9"),  # last hop wins
    ("::1", "2001:db8::7", "public", "2001:db8::7"),
    ("127.0.0.1", None, "public", "127.0.0.1"),
    ("127.0.0.1", "not-an-ip", "public", "127.0.0.1"),
    ("127.0.0.1", "203.0.113.9", "wireguard", "127.0.0.1"),       # no proxy there
    ("127.0.0.1", "203.0.113.9", "local", "127.0.0.1"),
    (None, None, "public", "unknown"),
])
def test_client_ip(peer, xff, mode, want):
    assert client_ip(peer, xff, mode) == want


def test_ten_failed_bearers_in_ten_minutes_ban_for_thirty():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    for _ in range(9):
        rl.auth_failed("203.0.113.9")
    assert rl.banned("203.0.113.9") == 0
    rl.auth_failed("203.0.113.9")
    assert rl.banned("203.0.113.9") == 1800
    assert rl.banned("198.51.100.4") == 0  # nobody else
    clock.now += 1799
    assert rl.banned("203.0.113.9") == 1
    clock.now += 1
    assert rl.banned("203.0.113.9") == 0


def test_failures_older_than_the_window_do_not_count():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    for _ in range(9):
        rl.auth_failed("203.0.113.9")
    clock.now += 601
    rl.auth_failed("203.0.113.9")
    assert rl.banned("203.0.113.9") == 0


def test_pair_allows_five_attempts_per_ten_minutes():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    for i in range(5):
        assert rl.pair_attempt("203.0.113.9") == 0
        clock.now += 10
    retry = rl.pair_attempt("203.0.113.9")
    assert retry == 600 - 50
    assert rl.pair_attempt("198.51.100.4") == 0
    clock.now += retry
    assert rl.pair_attempt("203.0.113.9") == 0


def test_memory_is_bounded_under_a_spray_of_addresses():
    rl = RateLimiter(clock=Clock(), max_tracked=100)
    for i in range(5000):
        rl.auth_failed(f"10.0.{i // 256}.{i % 256}")
        rl.pair_attempt(f"10.1.{i // 256}.{i % 256}")
    assert rl.tracked() <= 300


def test_the_journal_line_is_fail2ban_shaped_and_clean(caplog):
    with caplog.at_level(logging.INFO, logger=ratelimit.LOGGER_NAME):
        log_failure("203.0.113.9", "/v1/agents\n evil=1", "bad_token")
    assert [r.getMessage() for r in caplog.records] == [
        "deck-auth-fail ip=203.0.113.9 route=/v1/agents??evil=1 reason=bad_token"]
    assert caplog.records[0].levelno >= logging.WARNING
