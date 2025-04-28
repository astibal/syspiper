import re
import unicodedata

"""
This file is taken out from my other proprietary repository and is re-licensed explicitly for
use in this project, under original license:

All rights reserved.

This software is proprietary. It is not licensed for public use, distribution, or modification.

You may not:
- use the code for commercial or non-commercial purposes,
- host, run or deploy it in any environment,
- modify, repackage, or redistribute it,
- or integrate it into other projects,

without the explicit, written permission of the author.

Source code may be made available for review or evaluation on request, but such availability does not constitute permission to use.

Unauthorized use will be considered a violation of intellectual property rights.

© 2025 Ales Stibal, astib@nobs.watch

"""

class FilterSetup:
    BRUTAL_ALLOWED_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_@."
    PASSWORD_SP_CHARS = "-_@.,;!#$%^&*+=?"
    PASSWORD_ALLOWED_CHARS = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
    ) + PASSWORD_SP_CHARS



import re
import unicodedata

def strip_diacritics(s: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

def filter_email(text: str) -> str:
    """Keep only safe email characters, forcibly lowercase."""
    text = strip_diacritics(text)
    text = re.sub(r"[^\w\.\-\+@]", "", text)
    return text.lower().strip()

def filter_url(text: str) -> str:
    """Keep only safe URL-like chars."""
    text = strip_diacritics(text)
    text = re.sub(r"[^\w\-._~:/?#\[\]@!$&'()*+,;=%]", "", text)
    return text.strip()

def filter_username(text: str) -> str:
    """Alphanumeric + underscore only, no spaces, max length 30."""
    text = strip_diacritics(text)
    text = re.sub(r"[^\w]", "", text)
    return text.strip()[:30]

def filter_hostname(text: str) -> str:
    """Letters, digits, dash and dots. No leading/trailing dots/dashes."""
    text = strip_diacritics(text)
    text = re.sub(r"[^a-zA-Z0-9\.\-]", "", text)
    text = re.sub(r"\.+", ".", text)
    text = text.strip(".-")
    return text.lower()

def filter_path_component(text: str) -> str:
    """Safe for URL or filesystem path component"""
    text = strip_diacritics(text)
    text = re.sub(r"[^\w\.-]", "", text)
    return text.strip(".-_/")[:64]


def whitelist_filter(text: str, filter_chars: str, lowercase=False) -> str:
    if not isinstance(text, str):
        return ""

    if not isinstance(filter_chars, str):
        raise ValueError("filter_chars must be a string")

    # Normalize and strip diacritics
    text = strip_diacritics(text)

    # Remove non-ASCII
    text = text.encode("ascii", "ignore").decode()

    # Final brutal: only allow known characters
    FILTER = str(filter_chars)

    text = "".join(c for c in text if c in FILTER)

    return text.strip() if not lowercase else text.strip().lower()


def password_filter(text: str) -> str:
    return whitelist_filter(text, filter_chars=FilterSetup.PASSWORD_ALLOWED_CHARS)


def brutal_filter(text: str, lowercase=False, additional_chars=None) -> str:
    return whitelist_filter(
        text, filter_chars=FilterSetup.BRUTAL_ALLOWED_CHARS + (additional_chars or ""), lowercase=lowercase
    )