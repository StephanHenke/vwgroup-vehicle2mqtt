"""Explicit HTTPS portal origins shared by configuration, login and API requests."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from .api_constants import BASE_URL, LEGACY_BASE_URL


def normalize_portal_base_url(value: str) -> str:
    message = "portal_base_url must be an HTTPS origin without credentials, path, query or fragment"
    if not isinstance(value, str) or any(char.isspace() or char == "\\" for char in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError(message)
        host = parsed.hostname.lower()
    except ValueError:
        raise ValueError(message) from None
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}"


def login_origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        return normalize_portal_base_url(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    except ValueError:
        return None


def trusted_portal_origins(base_url: str = BASE_URL) -> frozenset[str]:
    # Known migration aliases are explicit; a new host requires an operator's choice.
    return frozenset((BASE_URL, LEGACY_BASE_URL, normalize_portal_base_url(base_url)))
