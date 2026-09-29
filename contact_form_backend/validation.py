"""Field validation: required, max length, email and phone formats."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .config import FieldSpec

_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PHONE = re.compile(r"^\+?[0-9 ().\-/]+(?:\s*(?:x|ext\.?|#)\s*[0-9]{1,6})?$", re.IGNORECASE)


def clean_text(value: str, multiline: bool) -> str:
    """Normalise newlines, drop control characters, trim."""
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    keep = []
    for ch in value:
        if ch == "\n":
            keep.append("\n" if multiline else " ")
        elif ch == "\t":
            keep.append(" " if not multiline else "\t")
        elif unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\u200d":
            continue
        else:
            keep.append(ch)
    text = "".join(keep).strip()
    if not multiline:
        text = re.sub(r" {2,}", " ", text)
    return text


def normalize_email(value: str) -> Optional[str]:
    """The address in canonical form, or None when it is not a plain address.

    Only ``local@domain`` is accepted (no display names, comments or
    whitespace), so the result is safe to use in a Reply-To header. A
    non-ASCII domain is converted to its IDNA (xn--) form; a non-ASCII local
    part is rejected because it would need SMTPUTF8.
    """
    if not value or len(value) > 254 or value.count("@") != 1:
        return None
    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or not _LOCAL.match(local):
        return None
    domain = domain.rstrip(".").lower()
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    if labels[-1].isdigit():
        return None
    address = local + "@" + domain
    return address if len(address) <= 254 else None


def valid_phone(value: str) -> bool:
    if not _PHONE.match(value):
        return False
    main = re.split(r"(?i)x|ext|#", value)[0]
    digits = sum(ch.isdigit() for ch in main)
    return 7 <= digits <= 15


def validate(
    fields: Sequence[FieldSpec], data: Mapping[str, str]
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Check submitted ``data`` against the form's fields.

    Returns ``(clean, errors)``: ``clean`` holds only declared fields (in
    declaration order, empty optional ones left out); ``errors`` maps field
    names to ``required``, ``too_long``, ``invalid_email`` or ``invalid_phone``.
    Undeclared fields are ignored.
    """
    clean: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for spec in fields:
        raw = data.get(spec.name, "")
        value = clean_text(raw if isinstance(raw, str) else "", spec.type == "textarea")
        if not value:
            if spec.required:
                errors[spec.name] = "required"
            continue
        if len(value) > spec.limit:
            errors[spec.name] = "too_long"
            continue
        if spec.type == "email":
            address = normalize_email(value)
            if address is None:
                errors[spec.name] = "invalid_email"
                continue
            value = address
        elif spec.type == "phone" and not valid_phone(value):
            errors[spec.name] = "invalid_phone"
            continue
        clean[spec.name] = value
    return clean, errors
