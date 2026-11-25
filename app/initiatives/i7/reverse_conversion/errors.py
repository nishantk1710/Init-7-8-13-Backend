"""Errors of the Min-Max -> OAR recommendation feature.

Kept inside this package: it shares no exception hierarchy with the OAR -> Min-Max conversion.
"""


class ReverseConversionError(Exception):
    """Base class for every error this feature raises on purpose."""


class AnchorError(ReverseConversionError):
    """The calculation anchor could not be fixed safely.

    Raised instead of guessing: an anchor that includes a partial month, or one nobody can justify,
    would shift every consumption window by a month without any sign of it in the numbers.
    """
