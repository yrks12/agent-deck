"""Model-id helpers."""

from __future__ import annotations


def normalize(model: str) -> str:
    """`claude-opus-5[1m]` / `anthropic.claude-opus-5` -> `claude-opus-5`."""
    if not model:
        return ""
    model = model.split("[", 1)[0]
    if model.startswith("anthropic."):
        model = model[len("anthropic.") :]
    return model
