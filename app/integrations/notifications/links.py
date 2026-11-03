"""Turning an in-app path into a link an email can carry."""

from __future__ import annotations


def is_safe_path(path: str | None) -> bool:
    """True for a same-site path such as ``/approvals`` -- one leading slash, no scheme or host."""
    return bool(path) and path.startswith("/") and not path.startswith("//") and "://" not in path


def absolute_url(path: str | None, base_url: str | None) -> str | None:
    """``base_url`` + ``path``, or ``None`` when either is missing or the path is unsafe.

    The base comes from the ``NOTIFICATION_FRONTEND_BASE_URL`` setting; nothing is
    hardcoded here. The destination page sits behind the normal sign-in, which
    sends an unauthenticated visitor to the login page and back.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base or not is_safe_path(path):
        return None
    return f"{base}{path}"
