"""Shared upay / MTB Islamic DPS knowledge and wording.

The ONLY knowledge source is knowledge/upay_dps.md, generated from the public page by fetch_upay_kb.py.
"""
import os

KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge", "upay_dps.md")

_facts_cache = None

# Owner-provided background (2026-11-26), NOT on the public page: say it only when asked, never volunteer it.
OWNER_NOTES = (
    "## Who is behind upay, and who provides this DPS\n"
    "upay is a product of UCB (United Commercial Bank PLC). This MTB Islamic DPS is provided by Mutual Trust Bank (MTB) "
    "and is opened inside the upay app. Say this ONLY if the person asks who owns or runs upay, which bank is behind upay, "
    "or how upay and MTB are related. Never bring it up yourself, and say nothing else about UCB."
)


def load_facts() -> str:
    """Return the knowledge file text (cached for the life of the process)."""
    global _facts_cache
    if _facts_cache is None:
        with open(KB_PATH, encoding="utf-8") as f:
            _facts_cache = f.read().strip() + "\n\n" + OWNER_NOTES
    return _facts_cache


_phone_cache = None


def load_phone_facts() -> str:
    """Condensed facts for the phone prompt (re-sent on every Live turn, so size = cost). Falls back to the full file."""
    global _phone_cache
    if _phone_cache is None:
        try:
            with open(os.path.join(os.path.dirname(KB_PATH), "upay_dps_phone.md"), encoding="utf-8") as f:
                _phone_cache = f.read().strip() + "\n\n" + OWNER_NOTES
        except OSError:
            _phone_cache = load_facts()
    return _phone_cache


# What the agent says when it cannot answer. No phone number on purpose (owner's decision).
HANDOFF_LINE_EN = (
    "I don't have that information right now. I am noting down your question, and a customer service "
    "person will contact you soon about it."
)

CONTACT_INFO = (
    "Our customer service team will contact you soon. "
    "You can also open or manage MTB Islamic DPS yourself from the upay app."
)
