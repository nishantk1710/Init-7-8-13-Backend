"""Every router I13 imports is actually mounted, and every path it claims is served.

Why this file exists
---------------------
``app/api/i13/routes.py`` imported ``assistant_routes`` and never included it.
The import satisfied the linter, the module read as wired, and
``POST /api/i13/consumption-plans`` -- the FR-4 capture point the FRS calls the
single one (§3.1, D8) -- answered 404 for as long as it had existed. Nothing
caught it, because nothing was comparing what the module imports against what
the app serves.

``tests/test_write_paths.py`` would have caught the write half, and was in fact
failing on it. This file catches the read half too, and says which router is
missing rather than which path.

The structural check is the one that matters
---------------------------------------------
Asserting a list of expected paths would have to be updated by the same person
who forgot the mount. Comparing the module's own imports against the app's route
table needs nobody to remember anything: adding a router file and forgetting to
include it fails here, by name.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.api.i13 import routes as i13_routes
from app.core.config import get_settings
from app.main import app

client = TestClient(app)

#: What a sub-router's own paths are prefixed with once served.
API_PREFIX = f"{get_settings().api_prefix}{i13_routes.router.prefix}"


@pytest.fixture(scope="module")
def served_paths() -> set[str]:
    return set(client.get("/openapi.json").json()["paths"])


def _imported_router_modules() -> dict[str, APIRouter]:
    """Every module imported into ``routes.py`` that exposes an ``APIRouter``.

    Read off the module object rather than a hand-written list, so a new
    sub-router is covered the moment it is imported.
    """
    found: dict[str, APIRouter] = {}
    for name, value in vars(i13_routes).items():
        if not inspect.ismodule(value):
            continue
        candidate = getattr(value, "router", None)
        if isinstance(candidate, APIRouter):
            found[name] = candidate
    return found


def _resolved_paths_in_registration_order() -> list[str]:
    """Every route the app serves, in the order it was registered.

    FastAPI 0.141 does not flatten an ``include_router`` at include time -- it
    keeps a lazy ``_IncludedRouter`` -- so neither ``app.routes`` nor the
    OpenAPI spec answers "which of these two was registered first?". This walks
    the include tree to recover that order.

    The accumulated prefix is deliberately NOT asserted on: nested includes
    double-count it here, and the question this answers is about ORDER, not
    about the exact string. Callers match on a suffix.
    """
    def walk(routes) -> list[str]:
        out: list[str] = []
        for route in routes:
            if hasattr(route, "path"):
                out.append(route.path)
            elif type(route).__name__ == "_IncludedRouter":
                out.extend(walk(route.original_router.routes))
        return out

    return walk(app.routes)


class TestEveryImportedRouterIsMounted:
    def test_at_least_the_known_sub_routers_are_discovered(self) -> None:
        """Guards the guard: if the discovery above ever returns nothing, the
        real assertion below would pass vacuously."""
        discovered = _imported_router_modules()
        assert len(discovered) >= 12, (
            "Expected routes.py to import at least a dozen sub-router modules; "
            f"found {sorted(discovered)}. If the module layout changed, fix the "
            "discovery rather than lowering this number -- a vacuous pass here "
            "is exactly the failure this file exists to prevent."
        )

    def test_every_imported_router_is_mounted(self, served_paths: set[str]) -> None:
        """A router imported into routes.py and never included is a dead route.

        This is the regression for the missing ``assistant_routes`` mount.
        Checked against what the app actually SERVES, not against what the
        aggregator holds -- the two were different, and only the first one is
        what a caller gets.
        """
        unmounted = sorted(
            name
            for name, sub in _imported_router_modules().items()
            # A mounted sub-router contributes every path it declares; an
            # unmounted one contributes none. One is enough to prove the mount.
            if sub.routes
            and not any(
                # `route.path` already carries the sub-router's own prefix --
                # APIRouter(prefix=...) applies it at decoration time, so
                # adding sub.prefix here would double it for act/
                # quantity-suggestion and pass them off as unmounted.
                f"{API_PREFIX}{route.path}" in served_paths
                for route in sub.routes
                if hasattr(route, "path")
            )
        )

        assert not unmounted, (
            "These routers are imported into app/api/i13/routes.py but never "
            f"include_router'd, so every path they declare answers 404: {unmounted}"
        )


class TestTheWs7PathsAreServed:
    """The three that were dead, named individually.

    The structural test above would catch them again, but it reports a module
    name. These report the URL a caller would actually get a 404 from, which is
    what somebody debugging arrives with.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/i13/consumption-plans",
            "/api/i13/quantity-suggestion/compute",
        ],
    )
    def test_ws7_read_path_is_served(self, served_paths: set[str], path: str) -> None:
        assert path in served_paths

    def test_the_fr4_capture_path_accepts_a_post(self, served_paths: set[str]) -> None:
        """FR-4's write path specifically -- the one that was missing."""
        spec = client.get("/openapi.json").json()
        assert "post" in spec["paths"]["/api/i13/consumption-plans"]


class TestTheSuggestionPathsDoNotCollide:
    """``/quantity-suggestion/compute`` and ``/quantity-suggestion/{id}`` both
    exist, and the literal must not be swallowed by the parameter.

    Suggestion ids are ``uuid4().hex``, so "compute" can never be a real one --
    but FastAPI matches in registration order, not by specificity, so only the
    mount order in routes.py keeps them apart. That ordering is asserted here
    rather than left to a comment.
    """

    def test_both_paths_are_served(self, served_paths: set[str]) -> None:
        assert "/api/i13/quantity-suggestion/compute" in served_paths
        assert "/api/i13/quantity-suggestion/{suggestion_id}" in served_paths

    def test_the_literal_is_registered_before_the_parameter(self) -> None:
        paths = _resolved_paths_in_registration_order()

        def first_ending_with(suffix: str) -> int:
            for index, path in enumerate(paths):
                if path.endswith(suffix):
                    return index
            raise AssertionError(f"no registered route ends with {suffix!r}")

        literal = first_ending_with("/quantity-suggestion/compute")
        parameterised = first_ending_with("/quantity-suggestion/{suggestion_id}")
        assert literal < parameterised, (
            "/quantity-suggestion/compute must be registered BEFORE "
            "/quantity-suggestion/{suggestion_id}, or the store's get-by-id "
            "swallows it and FR-3's compute endpoint becomes unreachable. See "
            "the mounting note in app/api/i13/routes.py."
        )
