"""Handle normalisation shared by target files and inbound events.

Both sides go through the same functions, so a target file can be written the
way a person writes an address ("Jane Doe <Jane@Example.com>", "(215) 555-0100")
and still compare equal to what a provider puts on the wire.
"""
from __future__ import annotations

import re
from email.utils import parseaddr

HANDLE_KINDS = ("email", "e164", "platform_user", "label")
# When one message matches a target on several kinds, report the strongest.
KIND_PRIORITY = {kind: rank for rank, kind in enumerate(HANDLE_KINDS)}

_EMAIL = re.compile(r"^[^\s@<>()\[\],;:\"']+@[^\s@<>()\[\],;:\"']+\.[^\s@<>()\[\],;:\"']+$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-/]+$")


def norm_email(value: object) -> str | None:
    """Lowercased bare address, or None when the value is not an email address."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    _, address = parseaddr(text)
    address = (address or text).strip().strip("<>").lower()
    return address if _EMAIL.match(address) else None


def norm_e164(value: object, default_country: str = "1") -> str | None:
    """E.164 (+<digits>) by digit normalisation; None when the value is not a number.

    A 10-digit number is taken as North American (+1). Anything with letters
    (a contact name, 'anonymous') is not a number. A '<digits>@domain' handle
    (an SMS gateway or chat-app address) normalises its local part.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    for prefix in ("tel:", "sms:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if "@" in text:
        text = text.split("@", 1)[0]
    if not text or not _PHONE_CHARS.match(text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if text.startswith("00") and 10 <= len(digits) - 2 <= 15:
        return "+" + digits[2:]
    if len(digits) == 10:
        return "+" + default_country + digits
    if len(digits) == 11 and digits.startswith(default_country):
        return "+" + digits
    return None


def norm_label(value: object) -> str | None:
    """Casefolded, whitespace-collapsed display label; None when empty."""
    if not isinstance(value, str):
        return None
    label = " ".join(value.casefold().split())
    return label or None


def label_candidates(value: object) -> list[tuple[str, str | None]]:
    """(normalised label, group label) pairs a display value may stand for.

    Group-chat notifications title the sender '<group>: <name>'. The whole
    string is tried first (group None); then the part after the last ': ',
    reporting the group title it came from.
    """
    whole = norm_label(value)
    if whole is None:
        return []
    out: list[tuple[str, str | None]] = [(whole, None)]
    raw = value.strip()  # type: ignore[union-attr]
    if ": " in raw:
        group, name = raw.rsplit(": ", 1)
        label = norm_label(name)
        group = group.strip()
        if label and label != whole:
            out.append((label, group or None))
    return out


def norm_platform(value: object, provider: str | None = None) -> str | None:
    """'<platform>:<id>' casefolded.

    Target files write 'slack:U123'. On the wire a platform handle is the bare
    id, so the provider (the source type's entity) is prefixed unless the value
    already names it.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if provider is None:
        if ":" not in text:
            return None
        platform, ident = text.split(":", 1)
        platform, ident = platform.strip(), ident.strip()
        if not platform or not ident:
            return None
        return f"{platform}:{ident}".casefold()
    head = text.split(":", 1)[0].casefold() if ":" in text else None
    if head == provider.casefold():
        return text.casefold()
    return f"{provider}:{text}".casefold()


def normalise_target_handle(kind: str, value: object) -> str | None:
    """Normalise one handle from a target file by its declared kind."""
    if kind == "email":
        return norm_email(value)
    if kind == "e164":
        return norm_e164(value)
    if kind == "label":
        return norm_label(value)
    if kind == "platform":
        return norm_platform(value)
    raise ValueError(f"unknown handle kind {kind!r}")
