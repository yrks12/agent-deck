"""K2 pairing code + K3 stores: one-time, expiring, hashed at rest.

The vectors are the contract with the Swift parser (F1 keeps the same file);
the stores are where a leaked screenshot, a second redemption or a revoked
laptop is either stopped or not.
"""

import json
import re
import stat
import threading

import pytest

from server import pairing
from server.pairing import DeviceStore, PairingError, PairingStore, encode, parse

from tests.conftest import FIXTURES, REPO

VECTORS_PATH = FIXTURES / "pairing_vectors.json"
#: F1's copy. Both parsers are graded against the same bytes or the contract
#: has quietly forked.
SWIFT_VECTORS = REPO / "macos/Tests/DeckKitTests/Fixtures/pairing_vectors.json"
VECTORS = {v["id"]: v for v in json.loads(VECTORS_PATH.read_text())["vectors"]}
VALID = [v for v in VECTORS.values() if "parsed" in v]
INVALID = [v for v in VECTORS.values() if "rejected" in v]
B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class Clock:
    def __init__(self, now=1_790_812_000.0):
        self.now = now

    def __call__(self):
        return self.now


# ── the code format ─────────────────────────────────────────────────────────


def test_the_vectors_are_the_plans_five():
    assert sorted(VECTORS) == ["V1", "V2", "V3", "V4", "V5"]
    assert VECTORS["V3"]["code"].replace("\n", "") == VECTORS["V1"]["code"]
    assert (VECTORS["V4"]["rejected"], VECTORS["V5"]["rejected"]) == (
        "code_incomplete", "code_version")


def test_server_and_app_vectors_are_byte_identical():
    if not SWIFT_VECTORS.exists():
        pytest.skip("macos/Tests/DeckKitTests/Fixtures/pairing_vectors.json is not on "
                    "this branch yet (lands with slice F1); parity is checked once it is")
    assert VECTORS_PATH.read_bytes() == SWIFT_VECTORS.read_bytes()


@pytest.mark.parametrize("vec", VALID, ids=lambda v: v["id"])
def test_valid_vectors_parse(vec):
    got = parse(vec["code"])
    want = vec["parsed"]
    assert (got.v, got.url, got.code, got.exp, got.tls, got.name) == (
        want["v"], want["url"], want["code"], want["exp"], want["tls"], want["name"])
    assert got.pin == want.get("pin", "")


@pytest.mark.parametrize("vid", ["V1", "V2"])
def test_encode_reproduces_the_vectors_byte_for_byte(vid):
    assert encode(parse(VECTORS[vid]["code"])) == VECTORS[vid]["code"]


@pytest.mark.parametrize("vec", INVALID, ids=lambda v: v["id"])
def test_invalid_vectors_are_refused_with_their_reason(vec):
    with pytest.raises(PairingError) as err:
        parse(vec["code"])
    assert err.value.reason == vec["rejected"]


def test_a_newer_code_says_update_the_app():
    with pytest.raises(PairingError) as err:
        parse(VECTORS["V5"]["code"])
    assert err.value.detail == "This code is from a newer Agent Deck — update the app."
    raw = dict(VECTORS["V1"]["parsed"], v=2)
    with pytest.raises(PairingError) as err:
        parse("ADK1." + pairing._b64encode(json.dumps(raw).encode()))
    assert err.value.reason == "code_version"


def test_lowercase_prefix_whitespace_and_extra_keys_are_tolerated():
    text = VECTORS["V1"]["code"]
    assert parse("  adk1." + text[5:12] + "\r\n\t" + text[12:] + " \n") == parse(text)
    raw = dict(VECTORS["V1"]["parsed"], bridge={"future": True})
    assert parse("ADK1." + pairing._b64encode(json.dumps(raw).encode())).code == raw["code"]


def _with(**changes):
    raw = dict(VECTORS["V1"]["parsed"], **changes)
    raw = {k: v for k, v in raw.items() if v is not None}
    return "ADK1." + pairing._b64encode(json.dumps(raw).encode())


@pytest.mark.parametrize("text", [
    "hello", "", "ADK1.", "ADK1.!!!!", "ADK1." + pairing._b64encode(b"not json"),
    "ADK1." + pairing._b64encode(b"[1]"),
    _with(url="http://203-0-113-7.sslip.io"),
    _with(url="https://203-0-113-7.sslip.io/v1"),
    _with(code="short"), _with(code="q7Zb0cV2l8RkT1xYw3HnA+"),
    _with(exp="soon"), _with(tls="none"), _with(name="x" * 61),
    _with(pin="sha256/47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="),  # pin with tls=ca
    _with(tls="pin", pin="md5/abc"),
])
def test_malformed_codes_are_refused(text):
    with pytest.raises(PairingError) as err:
        parse(text)
    assert err.value.reason in {"code_malformed", "code_incomplete"}


def test_a_missing_field_is_incomplete():
    with pytest.raises(PairingError) as err:
        parse(_with(code=None))
    assert err.value.reason == "code_incomplete"


# ── the pairing store ───────────────────────────────────────────────────────


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_mint_is_128_bits_hashed_at_rest_and_private(tmp_path):
    state = tmp_path / "state"
    store = PairingStore(state, clock=Clock())
    secret, exp = store.mint()
    assert len(secret) == 22 and B64URL.match(secret)
    assert exp == 1_790_812_000 + 900
    on_disk = (state / "pairing.json").read_text()
    assert secret not in on_disk
    assert json.loads(on_disk)["codes"][0]["sha256"] == pairing.sha256_hex(secret)
    assert _mode(state / "pairing.json") == 0o600 and _mode(state) == 0o700


def test_a_code_works_exactly_once(tmp_path):
    store = PairingStore(tmp_path, clock=Clock())
    secret, _ = store.mint()
    store.redeem(secret)
    with pytest.raises(PairingError) as err:
        store.redeem(secret)
    assert err.value.reason == "pair_used"


def test_unknown_and_expired(tmp_path):
    clock = Clock()
    store = PairingStore(tmp_path, clock=clock)
    secret, exp = store.mint()
    with pytest.raises(PairingError) as err:
        store.redeem("A" * 22)
    assert err.value.reason == "pair_unknown"
    clock.now = exp
    with pytest.raises(PairingError) as err:
        store.redeem(secret)
    assert err.value.reason == "pair_expired"


def test_a_sixth_code_evicts_the_oldest(tmp_path):
    clock = Clock()
    store = PairingStore(tmp_path, clock=clock)
    secrets = []
    for _ in range(6):
        secrets.append(store.mint()[0])
        clock.now += 1
    with pytest.raises(PairingError) as err:
        store.redeem(secrets[0])
    assert err.value.reason == "pair_unknown"
    for s in secrets[1:]:
        store.redeem(s)


def test_a_second_process_cannot_resurrect_a_used_code(tmp_path):
    """deckctl mints in its own process while the daemon redeems: a stale
    in-memory copy written back would make a burnt code live again."""
    daemon, cli = PairingStore(tmp_path, clock=Clock()), PairingStore(tmp_path, clock=Clock())
    secret, _ = cli.mint()
    daemon.redeem(secret)
    cli.mint()
    with pytest.raises(PairingError) as err:
        daemon.redeem(secret)
    assert err.value.reason == "pair_used"


def test_concurrent_redemptions_admit_exactly_one(tmp_path):
    secret, _ = PairingStore(tmp_path, clock=Clock()).mint()
    wins, reasons = [], []

    def go():
        try:
            PairingStore(tmp_path, clock=Clock()).redeem(secret)
            wins.append(1)
        except PairingError as exc:
            reasons.append(exc.reason)

    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and set(reasons) == {"pair_used"}


def test_mint_code_builds_a_code_the_parser_accepts(tmp_path):
    store = PairingStore(tmp_path, clock=Clock())
    text, exp = pairing.mint_code(store, url="https://192.168.64.5", name="test vm",
                                  tls="pin", pin="sha256/47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")
    code = parse(text)
    assert (code.url, code.exp, code.tls) == ("https://192.168.64.5", exp, "pin")
    store.redeem(code.code)


# ── device tokens ───────────────────────────────────────────────────────────


def test_a_device_token_is_stored_only_as_its_hash(tmp_path):
    devices = DeviceStore(tmp_path, clock=Clock())
    token, device = devices.add("Dan's MacBook")
    assert re.fullmatch(r"adt_[A-Za-z0-9_-]{43}", token)
    assert re.fullmatch(r"d_[0-9a-f]{8}", device.id)
    raw = (tmp_path / "devices.json").read_text()
    assert token not in raw and token[4:] not in raw
    row = json.loads(raw)["devices"][0]
    assert row == {"id": device.id, "name": "Dan's MacBook", "sha256": pairing.sha256_hex(token),
                   "created": 1_790_812_000, "last_seen": 1_790_812_000}
    assert _mode(tmp_path / "devices.json") == 0o600


def test_verify_and_revoke(tmp_path):
    devices = DeviceStore(tmp_path, clock=Clock())
    token, device = devices.add("mac")
    other, _ = devices.add("mac 2")
    assert devices.verify(token).id == device.id
    assert devices.verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert devices.verify("not-a-device-token") is None
    assert devices.verify("") is None
    assert devices.revoke(device.id) is True
    assert devices.verify(token) is None
    assert devices.verify(other) is not None
    assert devices.revoke(device.id) is False
    assert [d.name for d in devices.list()] == ["mac 2"]


def test_last_seen_is_written_at_most_once_a_minute(tmp_path):
    clock = Clock()
    devices = DeviceStore(tmp_path, clock=clock)
    token, _ = devices.add("mac")
    path = tmp_path / "devices.json"
    before = path.read_bytes()
    clock.now += 59
    devices.verify(token)
    assert path.read_bytes() == before
    clock.now += 1
    devices.verify(token)
    assert json.loads(path.read_text())["devices"][0]["last_seen"] == 1_790_812_060


def test_a_revoke_from_another_process_is_not_undone_by_last_seen(tmp_path):
    clock = Clock()
    daemon, cli = DeviceStore(tmp_path, clock=clock), DeviceStore(tmp_path, clock=clock)
    token, device = daemon.add("mac")
    daemon.verify(token)
    cli.revoke(device.id)
    clock.now += 120
    assert daemon.verify(token) is None
    assert daemon.list() == []


def test_a_corrupt_devices_file_admits_nobody_and_does_not_raise(tmp_path):
    (tmp_path / "devices.json").write_text("{not json")
    assert DeviceStore(tmp_path, clock=Clock()).verify("adt_" + "A" * 43) is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "bad\nname", 7, None])
def test_a_bad_device_name_is_refused(tmp_path, name):
    with pytest.raises(ValueError):
        DeviceStore(tmp_path, clock=Clock()).add(name)
