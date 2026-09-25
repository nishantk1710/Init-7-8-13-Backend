"""What can go wrong talking to SAP, as distinct types.

The taxonomy exists so a caller can decide *without parsing a message* whether
retrying could possibly help. Every class answers one question:

    AuthError        credentials or token exchange failed -- retrying with the
                     same credentials cannot work
    TransientError   5xx or a network fault -- worth retrying with backoff
    NotFoundError    404 -- the path is wrong; retrying cannot fix a wrong URL
    RequestError     any other 4xx, usually a malformed filter -- not retried
    ContractError    SAP answered, but with a shape the contract says is
                     impossible. This is drift, and it must never be retried or
                     swallowed: it means our picture of SAP is out of date.

The one that is NOT here, deliberately
--------------------------------------
A silently ignored filter. `filter_support.csv` measured 208 filterable
properties: 124 honoured, 11 rejected with HTTP 500, and **62 ignored** -- SAP
drops the filter and returns HTTP 200 with the whole set. There is no status
code, no error, nothing to catch. It cannot be an exception class because
nothing raises; it has to be prevented before the call. See `filters.py`.
"""

from __future__ import annotations


class SapError(RuntimeError):
    """Base for every SAP failure. Catch this to catch them all."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        # Truncated: SAP error bodies can be whole HTML pages, and this ends up
        # in logs.
        self.body = (body or "")[:2000] or None

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (HTTP {self.status})" if self.status else base


class AuthError(SapError):
    """Credentials or token exchange failed. Not retryable."""


class TransientError(SapError):
    """A 5xx or network fault. Retryable with backoff."""


class NotFoundError(SapError):
    """A 404. The entity set or path does not exist. Never retried."""


class RequestError(SapError):
    """A 4xx that is none of the above -- usually a filter SAP will not accept."""


class ContractError(SapError):
    """SAP's response contradicts the generated contract. Drift, not a bug."""


class UnsupportedFilterError(SapError):
    """Refusing to send a filter SAP is known to ignore.

    Raised before the request, not after. Returning silently wrong data is worse
    than failing, and this is the only signal available -- SAP answers 200.
    """


def classify(status: int, body: str, context: str) -> SapError:
    """Map an HTTP status to the right error type.

    ``context`` is what the caller was doing ("MaterialPlantSet", "$metadata for
    ZMM_KPI02_TAB_SRV"), so the message says what failed rather than just how.
    """
    if status in (401, 403):
        return AuthError(f"{context}: authentication rejected", status=status, body=body)
    if status == 404:
        return NotFoundError(f"{context}: not found", status=status, body=body)
    if status >= 500:
        return TransientError(f"{context}: SAP returned a server error", status=status, body=body)
    if status >= 400:
        return RequestError(f"{context}: SAP rejected the request", status=status, body=body)
    return SapError(f"{context}: unexpected status", status=status, body=body)
