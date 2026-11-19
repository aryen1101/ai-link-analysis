"""Text helpers shared across services."""

# Typographic characters LLMs like to emit (non-breaking hyphens/spaces, zero-width chars)
# that render badly in many clients. Replaced with plain equivalents.
TEXT_FIXES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "", "⁠": "", "﻿": "",
})


def clean_text(text: str) -> str:
    return (text or "").translate(TEXT_FIXES)
