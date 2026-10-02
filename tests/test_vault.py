"""The vault: one door values come out of, and a marker everywhere else.

Written before `server/vault.py` exists.

What is being pinned is not "the agent cannot see the secret" — it can, it runs
a real shell with a real environment, and any test claiming otherwise would be
lying. What is pinned is narrower and true:

  * the value is on disk in exactly one file, mode 0600, and nowhere else;
  * `meta()` and every `repr()` it produces carry no part of it;
  * `env_for` is the only function that returns it, and only to a granted desk;
  * `redactor` removes it from anything a human or another agent reads.

Every "the secret is absent" assertion is paired with one proving the real value
*is* delivered on the granted path — an empty dict passes an absence test just
as well as a correct one does.

Nothing here is a real credential. `sk_live_FAKE...` is shaped like Stripe's and
is not one.
"""

import json
import os
import stat

import pytest

from server import vault

FAKE = "sk_live_" + "FAKE51H8sV2qNrPzXk9TdWmB4gY7cQaE0uLjR"  # split: not a real key
FAKE2 = "whsec_FAKE9zTn4LpQ2vXeK7mHb3RdC8sYaW1"


def stocked(tmp_path, **kw):
    """A vault holding one fake Stripe key. Returns (path, meta)."""
    path = tmp_path / "vault.json"
    fields = {
        "name": "STRIPE_LIVE_KEY",
        "value": FAKE,
        "description": ("Live sk_live key so VideoVend can create a £99 "
                        "payment link. Test keys will not be used."),
        "grants": ("videovend",),
    }
    fields.update(kw)
    return path, vault.put(path, **fields)


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ── the file ───────────────────────────────────────────────────────────────


def test_the_file_is_0600_and_stays_0600(tmp_path):
    path, _ = stocked(tmp_path)
    assert mode_of(path) == 0o600

    vault.grant(path, "STRIPE_LIVE_KEY", "studio")
    assert mode_of(path) == 0o600

    vault.revoke(path, "STRIPE_LIVE_KEY", "studio")
    assert mode_of(path) == 0o600

    vault.env_for(path, "videovend")
    assert mode_of(path) == 0o600

    vault.put(path, name="WEBHOOK_SECRET", value=FAKE2,
              description="Stripe webhook signing secret.", grants=())
    assert mode_of(path) == 0o600

    vault.forget(path, "WEBHOOK_SECRET")
    assert mode_of(path) == 0o600


def test_default_path_is_under_the_bus_dir():
    from server.paths import BUS_DIR
    assert vault.DEFAULT_PATH == BUS_DIR / "vault.json"


def test_the_value_is_stored_in_that_file_and_no_other(tmp_path):
    path, _ = stocked(tmp_path)

    # The good signal: it really is persisted, or nothing below means anything.
    assert FAKE in path.read_text()

    others = [p for p in tmp_path.rglob("*") if p.is_file() and p != path]
    for other in others:
        assert FAKE not in other.read_text(errors="ignore"), other


def test_put_never_returns_the_value(tmp_path):
    _, meta = stocked(tmp_path)
    assert meta.name == "STRIPE_LIVE_KEY"
    assert not any(FAKE[:12] in str(v) for v in vars(meta).values())


def test_nothing_is_logged_when_a_secret_is_stored(tmp_path, caplog):
    with caplog.at_level(0):
        path, _ = stocked(tmp_path)
        vault.env_for(path, "videovend")
    assert FAKE not in caplog.text
    assert FAKE[:12] not in caplog.text


# ── meta never leaks ───────────────────────────────────────────────────────


def test_meta_carries_the_description_and_grants_but_no_value(tmp_path):
    path, _ = stocked(tmp_path)
    rows = vault.meta(path)

    assert len(rows) == 1
    row = rows[0]
    # Present: everything a board needs to draw the card.
    assert row.name == "STRIPE_LIVE_KEY"
    assert "£99 payment link" in row.description
    assert row.grants == ("videovend",)
    assert row.created_at > 0
    assert row.last_used is None
    # Absent: the value, in whole or in part.
    assert not hasattr(row, "value")
    blob = json.dumps({k: str(v) for k, v in vars(row).items()})
    assert FAKE not in blob
    assert FAKE[8:24] not in blob


def test_the_repr_of_meta_contains_no_part_of_the_secret(tmp_path):
    path, _ = stocked(tmp_path)
    text = repr(vault.meta(path)[0])

    assert "STRIPE_LIVE_KEY" in text          # it is still identifiable
    assert FAKE not in text
    assert FAKE[8:24] not in text
    assert "sk_live" not in text


def test_meta_records_when_a_desk_last_took_it(tmp_path):
    path, _ = stocked(tmp_path)
    vault.env_for(path, "videovend", now=1700.0)
    assert vault.meta(path)[0].last_used == 1700.0


# ── env_for: the only door ─────────────────────────────────────────────────


def test_a_granted_desk_gets_the_real_value(tmp_path):
    path, _ = stocked(tmp_path)
    assert vault.env_for(path, "videovend") == {"STRIPE_LIVE_KEY": FAKE}


def test_an_ungranted_desk_gets_nothing(tmp_path):
    path, _ = stocked(tmp_path)
    assert vault.env_for(path, "acme-growth") == {}
    # paired with the good signal, so an always-empty bug cannot pass
    assert vault.env_for(path, "videovend") == {"STRIPE_LIVE_KEY": FAKE}


def test_env_for_hands_over_only_the_secrets_that_desk_was_granted(tmp_path):
    path, _ = stocked(tmp_path)
    vault.put(path, name="WEBHOOK_SECRET", value=FAKE2,
              description="Stripe webhook signing secret.",
              grants=("acme-growth",))

    assert vault.env_for(path, "videovend") == {"STRIPE_LIVE_KEY": FAKE}
    assert vault.env_for(path, "acme-growth") == {"WEBHOOK_SECRET": FAKE2}


def test_grant_opens_the_door_and_revoke_shuts_it(tmp_path):
    path, _ = stocked(tmp_path)

    meta = vault.grant(path, "STRIPE_LIVE_KEY", "studio")
    assert "studio" in meta.grants
    assert vault.env_for(path, "studio") == {"STRIPE_LIVE_KEY": FAKE}

    meta = vault.revoke(path, "STRIPE_LIVE_KEY", "studio")
    assert "studio" not in meta.grants
    assert vault.env_for(path, "studio") == {}
    # the original grant is untouched
    assert vault.env_for(path, "videovend") == {"STRIPE_LIVE_KEY": FAKE}


def test_granting_twice_does_not_duplicate(tmp_path):
    path, _ = stocked(tmp_path)
    vault.grant(path, "STRIPE_LIVE_KEY", "studio")
    meta = vault.grant(path, "STRIPE_LIVE_KEY", "studio")
    assert meta.grants.count("studio") == 1


def test_an_unknown_secret_is_a_KeyError_not_a_silent_pass(tmp_path):
    path, _ = stocked(tmp_path)
    with pytest.raises(KeyError):
        vault.grant(path, "NO_SUCH_KEY", "studio")
    with pytest.raises(KeyError):
        vault.revoke(path, "NO_SUCH_KEY", "studio")


def test_a_name_that_cannot_be_an_env_var_is_refused(tmp_path):
    path = tmp_path / "vault.json"
    with pytest.raises(ValueError):
        vault.put(path, name="stripe key", value=FAKE, description="x",
                  grants=())
    with pytest.raises(ValueError):
        vault.put(path, name="", value=FAKE, description="x", grants=())


def test_an_empty_value_is_refused(tmp_path):
    path = tmp_path / "vault.json"
    with pytest.raises(ValueError):
        vault.put(path, name="STRIPE_LIVE_KEY", value="", description="x",
                  grants=())


def test_put_replaces_a_rotated_key_in_place(tmp_path):
    path, _ = stocked(tmp_path)
    rotated = "sk_live_" + "FAKEr0t4t3d000000000000000000000"
    vault.put(path, name="STRIPE_LIVE_KEY", value=rotated,
              description="Rotated 2026-09-01.", grants=("videovend",))

    assert len(vault.meta(path)) == 1
    assert vault.env_for(path, "videovend") == {"STRIPE_LIVE_KEY": rotated}
    assert FAKE not in path.read_text()


def test_forget_removes_the_value_from_disk(tmp_path):
    path, _ = stocked(tmp_path)
    vault.forget(path, "STRIPE_LIVE_KEY")

    assert vault.meta(path) == []
    assert vault.env_for(path, "videovend") == {}
    assert FAKE not in path.read_text()


def test_forget_on_an_absent_name_is_a_no_op(tmp_path):
    path, _ = stocked(tmp_path)
    vault.forget(path, "NO_SUCH_KEY")
    assert [m.name for m in vault.meta(path)] == ["STRIPE_LIVE_KEY"]


def test_an_empty_vault_reads_as_empty(tmp_path):
    path = tmp_path / "vault.json"
    assert vault.meta(path) == []
    assert vault.env_for(path, "videovend") == {}


# ── the redactor ───────────────────────────────────────────────────────────


def test_the_redactor_catches_the_value_wherever_it_appears(tmp_path):
    path, _ = stocked(tmp_path)
    scrub = vault.redactor(path)

    mid = scrub(f"I set the key to {FAKE} and the charge went through.")
    assert FAKE not in mid
    assert "I set the key to" in mid
    assert "and the charge went through." in mid
    assert vault.MARKER_FOR("STRIPE_LIVE_KEY") in mid

    url = scrub(f"curl https://api.stripe.com/v1/x?key={FAKE}&amount=9900")
    assert FAKE not in url
    assert "https://api.stripe.com/v1/x?key=" in url
    assert "&amount=9900" in url

    blob = scrub(json.dumps({"api_key": FAKE, "amount": 9900}))
    assert FAKE not in blob
    assert "9900" in blob


def test_the_redactor_catches_every_stored_secret(tmp_path):
    path, _ = stocked(tmp_path)
    vault.put(path, name="WEBHOOK_SECRET", value=FAKE2,
              description="Stripe webhook signing secret.", grants=())
    scrub = vault.redactor(path)

    out = scrub(f"key={FAKE} sig={FAKE2}")
    assert FAKE not in out
    assert FAKE2 not in out
    assert vault.MARKER_FOR("STRIPE_LIVE_KEY") in out
    assert vault.MARKER_FOR("WEBHOOK_SECRET") in out


def test_the_redactor_leaves_ordinary_prose_alone(tmp_path):
    path, _ = stocked(tmp_path)
    scrub = vault.redactor(path)

    prose = ("Use the live key, not a test key — sk_live_ is the prefix and "
             "STRIPE_LIVE_KEY is the variable name.")
    assert scrub(prose) == prose

    near = f"the key {FAKE[:-1]}X is a different key"
    assert scrub(near) == near


def test_the_redactor_is_safe_on_an_empty_vault(tmp_path):
    scrub = vault.redactor(tmp_path / "vault.json")
    assert scrub("nothing to hide here") == "nothing to hide here"
    assert scrub("") == ""


def test_the_redactor_never_returns_a_marker_that_contains_the_value(tmp_path):
    path, _ = stocked(tmp_path)
    assert FAKE not in vault.MARKER_FOR("STRIPE_LIVE_KEY")
    assert "sk_live" not in vault.MARKER_FOR("STRIPE_LIVE_KEY")
