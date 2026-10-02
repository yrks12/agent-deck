"""Standing approvals: the policy store, its life cycle and its refusals."""

import json

import pytest

from server import standing
from server.standing import StandingError


@pytest.fixture
def path(tmp_path):
    return tmp_path / "standing.json"


def email(**more):
    return {"desk": "atlas", "kind": "send_email",
            "limits": {"count_per_day": 80, "recipients": ["@acme.com"],
                       "account": "Me@acme.com"}, **more}


def test_the_owner_creates_an_active_policy_that_survives_a_reload(path):
    made = standing.create(email(), by="owner", path=path, now=100.0)
    assert made.status == "active" and made.approved_at == 100.0
    assert made.id.startswith("sa_") and made.created_by == "owner"
    again = standing.load(path)
    assert again == [made]
    wire = made.wire()
    assert wire["limits"] == {"count_per_day": 80, "usd_per_day": None,
                              "recipients": ["@acme.com"],
                              "account": "me@acme.com"}


def test_a_desk_proposal_grants_nothing_until_the_owner_approves(path):
    made = standing.create(email(), by="atlas", status="proposed", path=path)
    assert made.status == "proposed" and not made.live()
    active = standing.approve(made.id, path=path, now=5.0)
    assert active.status == "active" and active.live(now=6.0)
    with pytest.raises(StandingError) as again:
        standing.approve(made.id, path=path)
    assert again.value.reason == "not_proposed"


def test_a_caller_cannot_set_its_own_status_or_id(path):
    made = standing.create(email(status="active", id="sa_mine"), by="atlas",
                           status="proposed", path=path)
    assert made.status == "proposed" and made.id != "sa_mine"


def test_revoke_ends_it_and_a_revoked_policy_cannot_be_edited(path):
    made = standing.create(email(), by="owner", path=path)
    gone = standing.revoke(made.id, path=path, now=9.0)
    assert gone.status == "revoked" and gone.revoked_at == 9.0
    assert not gone.live()
    for change in (lambda: standing.revoke(made.id, path=path),
                   lambda: standing.edit(made.id, {"note": "x"}, path=path)):
        with pytest.raises(StandingError) as refused:
            change()
        assert refused.value.reason == "revoked"


def test_an_expired_policy_is_not_live(path):
    made = standing.create(email(expires_at=50.0), by="owner", path=path)
    assert made.live(now=49.0) and not made.live(now=51.0)


def test_edit_changes_limits_but_never_who_or_status(path):
    made = standing.create(email(), by="owner", path=path)
    changed = standing.edit(made.id, {"limits": {"count_per_day": 10},
                                      "status": "proposed",
                                      "created_by": "x"}, path=path)
    assert changed.count_per_day == 10 and changed.status == "active"
    assert changed.created_by == "owner"
    with pytest.raises(StandingError) as refused:
        standing.edit(made.id, {"limits": {"count_per_day": -1}}, path=path)
    assert refused.value.reason == "bad_limit"


@pytest.mark.parametrize("fields,reason", [
    ({"desk": "atlas", "kind": "fly"}, "bad_kind"),
    ({"kind": "send_email", "limits": {"count_per_day": 3}}, "missing_field"),
    ({"desk": "atlas", "kind": "send_email"}, "no_limit"),
    ({"desk": "atlas", "kind": "spend_money",
      "limits": {"count_per_day": 3}}, "no_limit"),
    ({"desk": "atlas", "kind": "send_email",
      "limits": {"count_per_day": 0}}, "bad_limit"),
    ({"desk": "atlas", "kind": "send_email",
      "limits": {"count_per_day": "lots"}}, "bad_limit"),
    ({"desk": "atlas", "kind": "run_command", "pattern": "*",
      "limits": {"count_per_day": 3}}, "too_wide"),
    ({"desk": "atlas", "kind": "run_command", "pattern": "rm *",
      "limits": {"count_per_day": 3}}, "never_coverable"),
    ({"desk": "*", "kind": "call_api", "tool": "mcp__gmail__delete_message",
      "limits": {"count_per_day": 3}}, "never_coverable"),
    ({"desk": "*", "kind": "spend_money", "tool": "mcp__stripe__create_payout",
      "limits": {"usd_per_day": 3}}, "never_coverable"),
])
def test_what_can_never_be_honoured_is_refused_not_stored(path, fields, reason):
    with pytest.raises(StandingError) as refused:
        standing.create(fields, by="owner", path=path)
    assert refused.value.reason == reason
    assert standing.load(path) == []


def test_unknown_ids_are_404(path):
    for change in (standing.approve, standing.revoke):
        with pytest.raises(StandingError) as refused:
            change("sa_nope", path=path)
        assert (refused.value.status, refused.value.reason) == (
            404, "unknown_policy")


def test_an_unreadable_store_is_no_policies_not_a_grant(path):
    path.write_text("{not json")
    assert standing.load(path) == []
    path.write_text(json.dumps({"policies": [{"kind": "send_email"}]}))
    assert standing.load(path) == []
