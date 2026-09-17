"""Domain errors for Initiative 07.

Four types, because four things go wrong for genuinely different reasons and a
caller responds to each differently:

* ``ConfigurationError``    -- a value is present but wrong (ADI cutoff of -1).
* ``PolicyNotConfiguredError`` -- a value is absent and nobody has decided it yet.
* ``ContractError``         -- source data cannot form a valid canonical record.
* ``ValidationError``       -- a domain invariant was violated elsewhere.

The distinction between the first two is the important one. A negative ADI
cutoff is a typo someone can fix now; an unsigned service-level matrix is a
business decision nobody at Vedanta has made yet. Reporting both as "bad
config" would send an engineer looking for a bug that is really a blocked
sign-off, so the Solution Design's rule -- an unsigned policy blocks
recommendations rather than falling back to a default -- needs its own type.
"""


class I07Error(Exception):
    """Base for every Initiative 07 domain error."""


class ConfigurationError(I07Error):
    """A configured value is present but invalid."""


class PolicyNotConfiguredError(I07Error):
    """A required business policy has not been decided yet.

    Raised when calculation needs a value that Vedanta has not signed off --
    service levels, the Max Stock strategy. Never satisfied with a default:
    guessing a service level produces a plausible, wrong, and unattributable
    safety stock.
    """

    def __init__(self, policy: str, detail: str = "") -> None:
        self.policy = policy
        message = f"Required policy not configured: {policy}"
        if detail:
            message = f"{message}. {detail}"
        super().__init__(message)


class ContractError(I07Error):
    """Source data could not be expressed as a valid canonical contract."""


class ValidationError(I07Error):
    """A domain invariant was violated."""
