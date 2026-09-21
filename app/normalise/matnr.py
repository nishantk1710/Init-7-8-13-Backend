"""SAP material numbers, padded the way SAP pads them.

THE RULE, AND WHY IT IS NOT "PAD EVERYTHING TO 18"

SAP's ALPHA conversion exit left-pads a material number with zeros to its full
width **only when the value is entirely numeric**. An alphanumeric material
number is stored exactly as typed. So ``2000000270`` becomes
``000000002000000270``, while ``SPARE-12`` stays ``SPARE-12``.

Padding unconditionally would corrupt the alphanumeric ones into something that
matches nothing -- the same silent-empty-join this module exists to prevent,
introduced by the fix for it.

The live client has both shapes. From the 21-Sep sweep, MaterialSet's length
distribution was 1,978 rows at 18 characters and 62 spread across lengths 3 to
14, so the short ones are real and routine rather than an anomaly to ignore.

WHY THIS MATTERS MORE THAN IT LOOKS

    SELECT ... FROM marc JOIN mara ON marc.matnr = mara.matnr

If one side is padded and the other is not, that returns **zero rows**. Not an
error. Not a warning. An empty result indistinguishable from "there is no
matching data", which is a conclusion somebody will then act on.
"""

from __future__ import annotations

# MATNR is CHAR(18) in SAP and declared as maxLength 18 on MaterialSet.Matnr,
# which the contract tests assert.
MATNR_WIDTH = 18


def pad(value: str | None) -> str | None:
    """Left-pad a numeric material number to 18 characters.

    Returns ``None`` for null and for blank, because the raw layer stores both
    and neither is a material. Non-numeric values are returned stripped but
    otherwise untouched -- see the module docstring.

    Already-padded input is returned unchanged, so this is safe to apply twice.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not text.isdigit():
        # Alphanumeric. SAP does not pad these and neither do we.
        return text
    if len(text) >= MATNR_WIDTH:
        # Already padded, or longer than the declared width -- which would mean
        # the source is wrong about something. Truncating here would hide it.
        return text
    return text.rjust(MATNR_WIDTH, "0")


def strip_padding(value: str | None) -> str | None:
    """The human-readable form: ``000000002000000270`` -> ``2000000270``.

    For display and for matching against a spreadsheet or a report, where the
    short form is what people use. Never for joining -- join on the padded
    form, which is the one SAP's own tables agree on.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not text.isdigit():
        return text
    stripped = text.lstrip("0")
    # A material number of all zeros is not nothing; it is zero.
    return stripped or "0"


def same_material(left: str | None, right: str | None) -> bool:
    """Whether two material numbers refer to the same material.

    For comparing across sources that may disagree about padding -- a report
    against an extract, say. Inside the serving layer everything is padded on
    the way in, so a plain ``=`` is correct there and this is not needed.
    """
    return pad(left) == pad(right)
