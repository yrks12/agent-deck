"""What no standing approval may ever cover. PURE.

A standing approval is the owner saying "this desk may do X up to N times a
day without asking". Four classes stay with him whatever a policy says, and
this module is the one place that names them:

    money_transfer    money leaving to a third party: transfers, payouts,
                      refunds, payments, checkouts, wires
    delete_data       rm, DROP TABLE, DELETE FROM, force-push, gmail trash,
                      an HTTP DELETE, any tool whose verb is delete/remove
    credential_entry  typing a password, reading a key, printing a token --
                      everything `autoreview.HANDOFF_PATTERNS` already hands
                      back, reused rather than copied so the two floors
                      cannot drift apart
    account_security  2FA / MFA, passkeys, recovery codes, app passwords,
                      security settings

Over-matching here costs a card; under-matching costs money or data. So the
patterns lean wide, and an action that matches is put to the owner even when a
policy would have covered it.
"""

from __future__ import annotations

import re

from . import asking, autoreview

CATEGORIES = ("money_transfer", "delete_data", "credential_entry",
              "account_security")

_SECURITY = re.compile(
    r"two[ -]?factor|2fa\b|\bmfa\b|\btotp\b|2-step|two-step|authenticator"
    r"|passkey|recovery (code|email|phone|key)s?|app password"
    r"|security (setting|question|key)s?|account security"
    r"|myaccount\.google\.com/security|/settings/security"
    r"|change password|reset password|password reset|auth refresh"
    r"|admin:org")

_MONEY = re.compile(
    r"\b(transfers?|payouts?|refunds?|remit\w*|withdraw\w*|zelle|venmo"
    r"|paypal|wise|payments?|send money|wire|wire transfer|pay invoice"
    r"|checkout|pay)\b")

_DELETE = re.compile(
    r"(?<![\w.-])(rm|rmdir|shred|unlink|mkfs\w*)(?![\w-])"
    r"|(?<![\w-])(delete|destroy|purge|wipe|erase|trash|remove|archive)"
    r"(?![\w-])|\s-delete\b"
    r"|\bdrop\s+(table|database|schema|collection|index)\b"
    r"|\bdelete\s+from\b|\btruncate\s+(table\s+)?\w"
    r"|\bgit\s+push\b.*(--force|\s-f\b)|\bgit\s+reset\s+--hard"
    r"|\bgit\s+clean\s+-\w*f|\bgit\s+branch\s+-d\b|\bdd\b.*\bof=")

_CREDENTIAL = re.compile(
    r"password|passwd|secret|credential|api key|private key|\btoken\b"
    r"|keychain")


def _norm(text: str) -> str:
    """Lower case, `_` read as a space so `delete_message` is two words."""
    return str(text).lower().replace("_", " ")


def _field(tool_input: dict, *names: str) -> str:
    return " ".join(str(tool_input[n]) for n in names
                    if isinstance(tool_input.get(n), (str, int, float)))


def never_coverable(tool_name: str, tool_input: dict) -> str | None:
    """The category this call belongs to, or None when a policy may cover it.

    Two haystacks on purpose. Money and deletion are VERBS: read from the tool
    name, a shell command, a URL and an HTTP method -- never from an email
    body, or "please delete my old draft" in a message would block the send.
    Credentials and security settings are read from everything, because a
    password typed into a field arrives as a field value.
    """
    if not isinstance(tool_input, dict):
        tool_input = {}
    subject = autoreview.tool_subject(tool_name, tool_input)
    everything = _norm(f"{tool_name} {subject}")
    verbs = _norm(f"{tool_name} {_field(tool_input, 'command', 'url', 'method', 'http_method', 'action')}")

    if _SECURITY.search(everything):
        return "account_security"
    if _MONEY.search(verbs):
        return "money_transfer"
    if _DELETE.search(verbs):
        return "delete_data"
    if _CREDENTIAL.search(everything) or asking.is_handoff_subject(subject) \
            or asking.is_handoff_subject(tool_name):
        return "credential_entry"
    return None


def policy_targets(tool: str, pattern: str) -> str | None:
    """Would a policy for `tool` matching `pattern` reach into the floor?

    Asked when a policy is created or approved: one that NAMES a floor action
    is refused outright, rather than written and then never honoured.
    """
    sample = re.sub(r"[*?\[\]]", " ", str(pattern or "")).strip()
    tool = str(tool or "")
    name = re.sub(r"[*?\[\]]", " ", tool).strip()
    if tool == "Bash":
        return never_coverable("Bash", {"command": sample})
    return never_coverable(name, {"url": sample, "method": sample})
