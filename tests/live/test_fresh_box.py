"""Live acceptance for the easy-setup plan (docs/plans/2026-09-30-easy-setup.md, sections 1 and 7).

Written BEFORE the installer exists, so it fails today on purpose: deploy/install.sh,
deploy/render.py and the release pipeline are not there yet. Deselected by default like the
rest of tests/live (pytest.ini: `-m "not live"`); run with `pytest -m live tests/live/test_fresh_box.py`.

Two kinds of test:
  * artefact tests (need nothing external): the installer and its templates exist and the
    L0/L1/L2 harness scripts are syntactically sound.
  * box tests (need DECK_FRESH_URL, e.g. https://localhost or https://1-2-3-4.sslip.io):
    what a stranger's freshly installed box must answer over the public edge.
    DECK_FRESH_TOKEN (a wrong token is used for the rate-limit check; never logged).
"""
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "live" / "fresh_box"
URL = os.environ.get("DECK_FRESH_URL", "").rstrip("/")

needs_box = pytest.mark.skipif(not URL, reason="DECK_FRESH_URL not set (a fresh box to probe)")


def _get(path, headers=None):
    ctx = ssl.create_default_context()
    if os.environ.get("DECK_FRESH_INSECURE") == "1":  # self-signed L0/L1 boxes
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(URL + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---- artefacts: fail today because the installer slices have not landed -------------------

@pytest.mark.parametrize("rel", [
    "deploy/install.sh",
    "deploy/render.py",
    "deploy/templates/agentdeck.service.in",
    "deploy/templates/deckdoctor.service.in",
    "deploy/templates/Caddyfile.in",
    "tests/fixtures/owner-box",
    "VERSION",
])
def test_the_installer_artefacts_exist(rel):
    assert (ROOT / rel).exists(), f"{rel} is not there yet (easy-setup wave 1/2)"


@pytest.mark.parametrize("script", ["run-l0.sh", "run-l1-multipass.sh", "run-l2-vps.sh"])
def test_the_harness_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(HARNESS / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_systemd_dockerfile_is_a_bare_ubuntu_24_04():
    text = (HARNESS / "Dockerfile.systemd").read_text()
    assert "FROM ubuntu:24.04" in text and "/sbin/init" in text


def test_l0_end_to_end_in_a_systemd_container():
    r = subprocess.run(["bash", str(HARNESS / "run-l0.sh")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]


# ---- a fresh box, probed from outside ------------------------------------------------------

@needs_box
def test_healthz_is_open_and_says_ok():
    status, body = _get("/healthz")
    assert status == 200 and json.loads(body).get("ok") is True


@needs_box
def test_the_legacy_unauthenticated_state_route_is_not_exposed():
    assert _get("/api/state")[0] == 404


@needs_box
def test_a_wrong_bearer_is_401_and_the_eleventh_in_a_row_is_429():
    codes = [_get("/v1/agents", {"Authorization": f"Bearer wrong-{i}"})[0] for i in range(11)]
    assert codes[0] == 401, codes
    assert codes[-1] == 429, codes


@needs_box
def test_version_route_needs_a_bearer():
    assert _get("/v1/version")[0] == 401


@needs_box
def test_an_unknown_pair_code_is_refused_with_a_plain_error():
    req = urllib.request.Request(URL + "/v1/pair", data=json.dumps({"code": "nope"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    ctx = ssl.create_default_context()
    if os.environ.get("DECK_FRESH_INSECURE") == "1":
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        urllib.request.urlopen(req, timeout=15, context=ctx)
        pytest.fail("a bogus pair code was accepted")
    except urllib.error.HTTPError as e:
        assert e.code in (400, 401, 404, 410), e.code
