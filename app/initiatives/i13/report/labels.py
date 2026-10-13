"""The words the I13 screens use, for the report to use too.

VZI renamed the internal terms on the screens (frontend PR #33, agreed
2026-10-12): a ``NO_PLAN`` position reads "Non-AI", ``PLAN_BREACH`` reads
"Planned Overdue", an ``OPEN`` exception reads "Requester not identified". A
report that said "no plan" and "open" beside a dashboard that says "Non-AI" and
"Requester not identified" would read as a different set of numbers. So the
report speaks the screens' language, from this one table -- the template, the
workbook and the model's glossary all read it.

Mirror of ``src/features/initiative-13/utils/status-labels.ts`` and
``src/lib/shared-data/sap-plants.ts`` in the frontend. Change both together.
"""

from __future__ import annotations

STOCK_TYPE = {
    "FAST": "Fast-moving",
    "SLOW": "Slow-moving",
    "NON_MOVING": "Non-moving",
}

PLAN_STATUS = {
    "ON_PLAN": "On plan",
    "BELOW_PLAN": "Below plan",
    "ABOVE_PLAN": "Above plan",
    "NO_PLAN": "Non-AI",
}

EXCEPTION_STATUS = {
    "OPEN": "Requester not identified",
    "AWAITING_REQUESTER": "Assigned to requester",
    "ESCALATED": "Escalated to HOD",
    "CONFIRMED": "Reason given",
    "RESOLVED": "Closed",
}

EXCEPTION_TYPE = {
    "PLAN_BREACH": "Planned Overdue",
    "NO_PLAN": "Non-AI",
    "NO_PLAN_GRNI": "Non-AI + 30-day not issued",
    "QUANTITY_OVERRIDE": "Quantity Overridden",
}

#: As the I13 screens name them (``src/lib/shared-data/sap-plants.ts``) --
#: "Black Mountain", where ``app.shared.plant_scope`` says "Black Mountain
#: Mining". The report matches the screens it summarises.
PLANT_NAMES = {
    "1300": "Black Mountain",
    "1500": "Gamsberg",
}

#: Every internal code above. None of them may appear in the report's prose.
CODES = frozenset(STOCK_TYPE) | frozenset(PLAN_STATUS) | frozenset(EXCEPTION_STATUS) | frozenset(EXCEPTION_TYPE)


def plant_label(code: str | None) -> str:
    """``Black Mountain (1300)`` -- the name and the code, as the screens show it."""
    if not code:
        return "All plants"
    name = PLANT_NAMES.get(code)
    return f"{name} ({code})" if name else code


def glossary() -> str:
    """The code-to-word table, as the model is shown it."""
    lines = []
    for title, table in (
        ("Stock type (positions.FAST / SLOW / NON_MOVING)", STOCK_TYPE),
        ("Acquired vs plan (plans.acquired_vs_plan)", PLAN_STATUS),
        ("Exception status (exceptions.by_status, status)", EXCEPTION_STATUS),
        ("Exception type (exceptions.by_type, type)", EXCEPTION_TYPE),
    ):
        lines.append(f"{title}:")
        lines.extend(f"  {code} -> \"{word}\"" for code, word in table.items())
    return "\n".join(lines)
