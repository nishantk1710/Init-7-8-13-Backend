"""Every write route declares the roles allowed to call it; every read needs sign-in.

The sign-in layer (app/core/security.py) only protects what is declared, so
this file makes the declaration impossible to forget:

1. **Every non-GET route declares its roles** with ``require_roles(...)`` (or,
   for SAP's events, ``require_sap_events``) -- except the routes still listed
   in :data:`PENDING`, the initiative work in progress. A NEW write route
   without roles fails here at once.

2. **PENDING only shrinks.** When an initiative adds ``require_roles`` to one of
   its routes, this test fails until that line is deleted from PENDING, so the
   list is always the true remaining work. When it is empty, delete it and the
   rule is absolute.

3. **Every GET route carries the read rule** (``require_signed_in``), except
   the four deliberately open ones in :data:`OPEN_READS`.

Routes are read from the app as served -- including the dependencies their
routers are mounted with in ``app.api.router`` -- not from the route files.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.routing import iter_route_contexts

from app.core.security import ALL_ROLES
from app.main import app

#: Write routes whose initiative has not declared roles yet: (path, method) ->
#: owner. Delete a line in the same change that adds ``require_roles`` to it.
#: Target roles per route: RBAC task briefs (I07 Khushi, I08 Varad, I13 Shravani).
PENDING: dict[tuple[str, str], str] = {
    # --- I07: inventory planning (Khushi) ---
    ("/api/v1/i7/recommendations/{recommendation_id}/submit", "POST"): "I07",
    ("/api/v1/i7/recommendations/{recommendation_id}/reject", "POST"): "I07",
    ("/api/v1/i7/recommendations/{recommendation_id}/actions", "POST"): "I07",
    ("/api/v1/i7/recommendations/{recommendation_id}/similar-materials/exclusions", "POST"): "I07",
    ("/api/v1/i7/recommendations/{recommendation_id}/similar-materials/exclusions/restore", "POST"): "I07",
    ("/api/v1/i7/reports/quarterly/generate", "POST"): "I07",
    ("/api/v1/i7/kpi-formulas/apply", "POST"): "I07",
    ("/api/v1/i7/kpi-formulas/preview", "POST"): "I07",
    ("/api/v1/i7/kpi-formulas/reset", "POST"): "I07",
    ("/api/criticality/uploads", "POST"): "I07",
    # --- I08: repairable spares (Varad) ---
    ("/api/i8/attestations", "POST"): "I08",
    ("/api/i8/exceptions/{exception_id}/events", "POST"): "I08",
    # --- I13: OAR utilisation and the reservation assistant (Shravani) ---
    ("/api/i13/act/exceptions/{exception_id}/confirmation", "POST"): "I13",
    ("/api/i13/act/run/detect", "POST"): "I13",
    ("/api/i13/act/run/escalate", "POST"): "I13",
    ("/api/i13/consumption-plans", "POST"): "I13",
    ("/api/i13/quantity-suggestion", "POST"): "I13",
    ("/api/i13/quantity-suggestion/{suggestion_id}/justification", "POST"): "I13",
    ("/api/i13/quantity-suggestion/{suggestion_id}/acceptance", "POST"): "I13",
    ("/api/i13/reports/summary", "POST"): "I13",
    ("/api/i13/snapshot/refresh", "POST"): "I13",
    ("/api/i13/validation/zmm065/uploads", "POST"): "I13",
    ("/api/i13/uat/reservations/simulate", "POST"): "I13",
    ("/api/i13/uat/requests/simulate", "POST"): "I13",
    ("/api/i13/uat/reservations/stamp", "POST"): "I13",
    ("/api/i13/uat/reservations/{uat_id}/remove", "POST"): "I13",
    ("/api/assistant/sessions", "POST"): "I13",
    ("/api/assistant/sessions/{session_id}/turns", "POST"): "I13",
    ("/api/assistant/ask", "POST"): "I13",
    ("/api/assistant/requests", "POST"): "I13",
    ("/api/assistant/requests/preview", "POST"): "I13",
    ("/api/justifications", "POST"): "I13",
}

#: GET routes open to anyone: Azure's health probes, and /api/me, which checks
#: the token itself so a user without a role is told so rather than refused.
OPEN_READS = {"/", "/api/health", "/api/ready", "/api/me"}


def _routes() -> Iterator[tuple[str, str, frozenset[str], bool]]:
    """(path, method, declared roles, carries the read rule) for every served route."""
    for context in iter_route_contexts(app.routes):
        # The effective route includes the dependencies its routers were
        # mounted with; the original route only has its own.
        effective = getattr(context, "_effective_route", None) or context.route
        dependant = getattr(effective, "dependant", None)
        if dependant is None:
            continue
        roles: set[str] = set()
        signed_in = False
        stack = list(dependant.dependencies)
        while stack:
            sub = stack.pop()
            roles |= set(getattr(sub.call, "__required_roles__", ()) or ())
            signed_in |= bool(getattr(sub.call, "__signed_in_rule__", False))
            stack.extend(sub.dependencies)
        for method in sorted(set(context.methods or ()) - {"HEAD", "OPTIONS"}):
            yield context.path, method, frozenset(roles), signed_in


def _writes() -> dict[tuple[str, str], frozenset[str]]:
    return {(path, method): roles for path, method, roles, _ in _routes() if method != "GET"}


def test_routes_were_found():
    # Guards the helper itself: an API change in FastAPI must not make this
    # file pass by finding nothing.
    assert len(_writes()) >= 30
    assert sum(1 for _, method, _, _ in _routes() if method == "GET") >= 50


def test_every_write_route_declares_roles():
    undeclared = sorted(key for key, roles in _writes().items() if not roles and key not in PENDING)
    assert not undeclared, (
        "Write route(s) without require_roles(...) -- declare who may call them "
        f"(see app/core/security.py): {undeclared}"
    )


def test_pending_only_lists_routes_still_pending():
    writes = _writes()
    gone = sorted(key for key in PENDING if key not in writes)
    done = sorted(key for key in PENDING if key in writes and writes[key])
    assert not gone, f"PENDING names route(s) that no longer exist -- delete them: {gone}"
    assert not done, f"These now declare roles -- delete them from PENDING: {done}"


def test_declared_roles_are_real():
    for key, roles in _writes().items():
        assert roles <= ALL_ROLES, f"{key} declares unknown role(s) {sorted(roles - ALL_ROLES)}"


def test_sap_events_need_the_sap_role():
    writes = _writes()
    assert writes[("/api/events/pr", "POST")] == {"Sap.Events.Write"}
    assert writes[("/api/events/csv", "POST")] == {"Sap.Events.Write"}


def test_every_read_needs_sign_in():
    open_reads = sorted({path for path, method, _, signed_in in _routes() if method == "GET" and not signed_in})
    assert open_reads == sorted(OPEN_READS), (
        "GET route(s) outside the read rule -- mount their router in app/api/router.py "
        f"with dependencies=SIGNED_IN: {sorted(set(open_reads) - OPEN_READS)}"
    )


def test_writes_also_carry_the_read_rule():
    # A person needs at least one role for anything; only SAP's events differ.
    missing = sorted(
        (path, method)
        for path, method, _, signed_in in _routes()
        if method != "GET" and not signed_in and not path.startswith("/api/events/")
    )
    assert not missing, f"Write route(s) outside the read rule: {missing}"
