"""The never-coverable floor of standing approvals.

A standing approval lets a desk act without a card. Four classes must ALWAYS
come back to the owner whatever a policy says: money leaving to a third party,
deleting data, entering a credential, and changing account security. These
are the examples that must be caught, and the ordinary work that must not be.
"""

import pytest

from server import standing_floor as floor


def bash(command: str) -> tuple[str, dict]:
    return "Bash", {"command": command}


NEVER = [
    # deleting data
    (bash("rm -rf build"), "delete_data"),
    (bash("rm notes.txt"), "delete_data"),
    (bash("ls && rm -f a.log"), "delete_data"),
    (bash("find . -name '*.tmp' -delete"), "delete_data"),
    (bash("shred -u secrets.bin"), "delete_data"),
    (bash("rmdir old"), "delete_data"),
    (bash("psql -c 'DROP TABLE users'"), "delete_data"),
    (bash("psql -c 'drop database prod'"), "delete_data"),
    (bash("sqlite3 a.db 'DELETE FROM orders'"), "delete_data"),
    (bash("psql -c 'TRUNCATE TABLE logs'"), "delete_data"),
    (bash("git push --force origin main"), "delete_data"),
    (bash("git push -f origin main"), "delete_data"),
    (bash("git reset --hard HEAD~3"), "delete_data"),
    (bash("git clean -fdx"), "delete_data"),
    (bash("git branch -D feature"), "delete_data"),
    (bash("kubectl delete pod web-1"), "delete_data"),
    (bash("aws s3 rm s3://bucket/key --recursive"), "delete_data"),
    (bash("docker volume rm data"), "delete_data"),
    (bash("curl -X DELETE https://api.acme.com/v1/users/7"), "delete_data"),
    (bash("dd if=/dev/zero of=/dev/disk2"), "delete_data"),
    (("mcp__claude_ai_Gmail__trash_message", {"messageId": "x"}),
     "delete_data"),
    (("mcp__gmail__delete_message", {"id": "x"}), "delete_data"),
    (("mcp__claude_ai_Google_Calendar__delete_event", {"eventId": "e"}),
     "delete_data"),
    (("mcp__drive__remove_file", {"fileId": "f"}), "delete_data"),
    (("mcp__notion__archive_page", {"page": "p"}), "delete_data"),
    (("mcp__api__request", {"method": "DELETE", "url": "https://x.io/a"}),
     "delete_data"),
    # money to third parties
    (("mcp__stripe__create_transfer", {"amount": 500}), "money_transfer"),
    (("mcp__stripe__create_payout", {"amount": 500}), "money_transfer"),
    (("mcp__stripe__create_refund", {"charge": "ch_1"}), "money_transfer"),
    (bash("stripe transfers create --amount 500 --destination acct_1"),
     "money_transfer"),
    (bash("stripe payouts create --amount 100"), "money_transfer"),
    (("mcp__paypal__send_money", {"to": "a@b.c"}), "money_transfer"),
    (("mcp__bank__wire_transfer", {"iban": "GB00"}), "money_transfer"),
    (("mcp__wise__create_payment", {"amount": 3}), "money_transfer"),
    (("mcp__venmo__pay", {"user": "bob"}), "money_transfer"),
    (("mcp__quickbooks__pay_invoice", {"invoice": "9"}), "money_transfer"),
    (("WebFetch", {"url": "https://api.stripe.com/v1/transfers"}),
     "money_transfer"),
    (("mcp__shop__checkout", {"cart": "c"}), "money_transfer"),
    # credentials
    (("mcp__computer__type_text",
      {"selector": "input[type=password]", "text": "hunter2"}),
     "credential_entry"),
    (("mcp__claude-in-chrome__form_input",
      {"ref": "password-field", "value": "x"}), "credential_entry"),
    (bash("cat ~/.ssh/id_ed25519"), "credential_entry"),
    (bash("cat .env"), "credential_entry"),
    (bash("security find-generic-password -s github"), "credential_entry"),
    (bash("gcloud auth print-access-token"), "credential_entry"),
    (("Read", {"file_path": "/home/a/.aws/credentials"}),
     "credential_entry"),
    (("mcp__vault__get_secret", {"name": "db"}), "credential_entry"),
    # account security
    (("mcp__google__update_security_settings", {"x": 1}),
     "account_security"),
    (("mcp__github__enable_two_factor", {}), "account_security"),
    (("mcp__computer__click", {"label": "Turn off 2-Step Verification"}),
     "account_security"),
    (("mcp__auth__reset_mfa", {"user": "me"}), "account_security"),
    (("WebFetch", {"url": "https://myaccount.google.com/security"}),
     "account_security"),
    (("mcp__computer__click", {"label": "Show recovery codes"}),
     "account_security"),
    (("mcp__x__add_passkey", {}), "account_security"),
    (("mcp__x__create_app_password", {}), "account_security"),
    (bash("gh auth refresh -s admin:org"), "account_security"),
    (bash("gh ssh-key add key.pub"), "credential_entry"),
]

COVERABLE = [
    bash("gh pr list"),
    bash("git status && git diff"),
    bash("npm run build"),
    bash("ls -la"),
    bash("grep -rn transform src"),   # `rm` inside a word is not rm
    bash("python -m pytest -q"),
    bash("git log --format=%H"),
    ("mcp__claude_ai_Gmail__send_message",
     {"to": "bob@acme.com", "subject": "Weekly update", "body": "Hi Bob"}),
    ("mcp__github__add_issue_comment", {"issue": 3, "body": "Looks good"}),
    ("WebFetch", {"url": "https://api.acme.com/v1/orders"}),
    ("mcp__fal__generate_image", {"prompt": "a cat", "cost_usd": 0.04}),
    ("mcp__api__request", {"method": "GET", "url": "https://x.io/a"}),
]


@pytest.mark.parametrize("call,category", NEVER,
                         ids=[f"{c[0][0]}:{i}" for i, c in enumerate(NEVER)])
def test_every_never_coverable_action_is_caught(call, category):
    tool, tool_input = call
    assert floor.never_coverable(tool, tool_input) == category


@pytest.mark.parametrize("call", COVERABLE,
                         ids=[f"{c[0]}:{i}" for i, c in enumerate(COVERABLE)])
def test_ordinary_work_is_not_on_the_floor(call):
    tool, tool_input = call
    assert floor.never_coverable(tool, tool_input) is None


def test_the_floor_is_the_existing_handoff_floor_and_more():
    """No drift: anything the deck already hands back is on this floor too."""
    from server.autoreview import HANDOFF_PATTERNS

    for pattern in HANDOFF_PATTERNS:
        example = pattern.replace("*", " x ").strip()
        assert floor.never_coverable("Bash", {"command": example}) is not None, \
            pattern


@pytest.mark.parametrize("tool,pattern", [
    ("Bash", "rm *"),
    ("Bash", "git push --force*"),
    ("mcp__gmail__delete_message", "*"),
    ("mcp__stripe__create_transfer", "*"),
    ("Bash", "psql -c 'DROP TABLE*"),
])
def test_a_policy_that_targets_the_floor_is_named(tool, pattern):
    assert floor.policy_targets(tool, pattern) is not None


def test_a_policy_for_ordinary_work_does_not_target_the_floor():
    assert floor.policy_targets("Bash", "gh pr*") is None
    assert floor.policy_targets("mcp__claude_ai_Gmail__send_message", "*") is None
