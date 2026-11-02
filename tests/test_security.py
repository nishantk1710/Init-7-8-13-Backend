"""Sign-in and role checks (app/core/security.py), with test-signed tokens.

Three layers are covered: token validation (every way a token can be wrong),
the dependencies routes use (require_roles, require_signed_in,
require_sap_events, the actor helpers), and the wiring on the real app (open
health checks, /api/me, the read rule on business routes). Plus the user
directory that turns an object ID back into a name.

Sign-in off is tested as carefully as sign-in on: it is the default everywhere
and the rollback, so it must behave exactly as before sign-in existed.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deps as shared_deps
from app.api.i13 import deps as i13_deps
from app.core import security, user_directory
from app.core.security import ALL_ROLES, USER_ROLES, CurrentUser, Roles
from app.main import app
from app.models.app_user import AppUser
from app.models.base import Base
from tests.auth_helpers import (
    OTHER_PRIVATE_KEY,
    TEST_CLIENT_ID,
    TEST_TENANT,
    TEST_USER_OID,
    auth_headers,
    make_token,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# A small app exercising the dependencies on their own (no database needed)
# ---------------------------------------------------------------------------

probe = FastAPI()


@probe.get("/read", dependencies=[Depends(security.require_signed_in)])
def _read() -> dict:
    return {"ok": True}


@probe.post("/stores")
def _stores(user: CurrentUser = Depends(security.require_roles(Roles.STORES))) -> dict:
    return {"oid": user.oid, "authenticated": user.authenticated}


@probe.post("/ic-or-admin", dependencies=[Depends(security.require_roles(Roles.INVENTORY_CONTROLLER, Roles.ADMIN))])
def _ic_or_admin() -> dict:
    return {"ok": True}


@probe.post("/sap")
def _sap(caller: CurrentUser | None = Depends(security.require_sap_events)) -> dict:
    return {"caller": caller.oid if caller else None}


@probe.post("/actor/shared")
def _shared_actor(actor: shared_deps.Actor = Depends(shared_deps.get_current_actor)) -> dict:
    return {"actor": actor.id}


@probe.post("/actor/i13")
def _i13_actor(actor: i13_deps.Actor = Depends(i13_deps.get_current_actor)) -> dict:
    return {"actor": actor.id}


@probe.post("/twice")
def _twice(
    a: CurrentUser = Depends(security.require_signed_in),
    b: CurrentUser = Depends(security.require_roles(Roles.VIEWER)),
) -> dict:
    return {"same": a is b}


probe_client = TestClient(probe)


# ---------------------------------------------------------------------------
# Sign-in OFF -- the default, and the rollback
# ---------------------------------------------------------------------------


class TestSignInOff:
    def test_nothing_is_checked(self):
        assert probe_client.get("/read").status_code == 200
        assert probe_client.post("/stores").status_code == 200
        assert probe_client.post("/ic-or-admin").status_code == 200

    def test_actor_is_the_header_as_before(self):
        assert probe_client.post("/actor/shared", headers={"X-Actor-Id": " alice "}).json() == {"actor": "alice"}
        assert probe_client.post("/actor/i13", headers={"X-Actor-Id": "bob"}).json() == {"actor": "bob"}
        assert probe_client.post("/actor/shared").json() == {"actor": "unknown-actor"}
        assert probe_client.post("/actor/i13").json() == {"actor": "unknown-actor"}

    def test_a_token_is_not_even_read(self):
        # A broken token must not matter while sign-in is off.
        r = probe_client.post("/stores", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 200 and r.json() == {"oid": "unknown-actor", "authenticated": False}

    def test_me_reports_sign_in_off_with_every_role(self):
        r = client.get("/api/me", headers={"X-Actor-Id": "carol"})
        assert r.status_code == 200
        body = r.json()
        assert body["authEnabled"] is False and body["oid"] == "carol"
        assert set(body["roles"]) == USER_ROLES

    def test_sap_events_open_by_default(self):
        assert probe_client.post("/sap").json() == {"caller": None}

    def test_business_reads_are_not_blocked(self):
        # The read rule is on the router, but lets everything through while off.
        assert client.get("/api/me").status_code == 200
        assert client.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# Sign-in ON -- token validation, through the real app's /api/me
# ---------------------------------------------------------------------------


class TestTokenValidation:
    def test_valid_token(self, auth_enabled):
        r = client.get("/api/me", headers=auth_headers(roles=[Roles.VIEWER, Roles.ADMIN]))
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "oid": TEST_USER_OID,
            "name": "Test User",
            "email": "test.user@example.com",
            "roles": [Roles.ADMIN, Roles.VIEWER],
            "authEnabled": True,
            "isApplication": False,
        }

    def test_api_uri_audience_also_accepted(self, auth_enabled):
        r = client.get("/api/me", headers=auth_headers(aud=f"api://{TEST_CLIENT_ID}"))
        assert r.status_code == 200

    def test_extra_audience_setting(self, auth_enabled, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("AUTH_AUDIENCE", "https://spares.example/api, other")
        get_settings.cache_clear()
        assert client.get("/api/me", headers=auth_headers(aud="other")).status_code == 200

    def test_no_token(self, auth_enabled):
        r = client.get("/api/me")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Bearer")

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer",
            "Basic dXNlcjpwYXNz",
            "Bearer not.a.jwt",
            "Bearer " + "x" * 40,
        ],
    )
    def test_malformed(self, auth_enabled, header):
        assert client.get("/api/me", headers={"Authorization": header}).status_code == 401

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"aud": "33333333-0000-0000-0000-000000000000"},  # another API's token
            {"iss": "https://login.microsoftonline.com/someone-else/v2.0"},
            {"iss": f"https://sts.windows.net/{TEST_TENANT}/"},  # v1 token
            {"tid": "44444444-4444-4444-4444-444444444444"},
            {"expires_in": -600},  # expired beyond the clock skew
            {"key": OTHER_PRIVATE_KEY},  # forged signature
            {"scp": "User.Read"},  # a user token for some other purpose
        ],
        ids=["audience", "issuer", "v1-issuer", "tenant", "expired", "forged", "scope"],
    )
    def test_rejected(self, auth_enabled, kwargs):
        assert client.get("/api/me", headers=auth_headers(**kwargs)).status_code == 401

    def test_slightly_expired_within_clock_skew(self, auth_enabled):
        assert client.get("/api/me", headers=auth_headers(expires_in=-30)).status_code == 200

    def test_alg_none_is_rejected(self, auth_enabled):
        import jwt

        token = jwt.encode({"oid": TEST_USER_OID}, key=None, algorithm="none")
        assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_application_token(self, auth_enabled):
        r = client.get("/api/me", headers=auth_headers(roles=[Roles.SAP_EVENTS_WRITE], application=True))
        assert r.status_code == 200
        assert r.json()["isApplication"] is True

    def test_signed_in_without_roles_still_gets_me(self, auth_enabled):
        r = client.get("/api/me", headers=auth_headers(roles=[]))
        assert r.status_code == 200 and r.json()["roles"] == []

    def test_misconfigured_fails_closed(self, auth_enabled, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("ENTRA_TENANT_ID", "")
        get_settings.cache_clear()
        r = client.get("/api/me", headers=auth_headers())
        assert r.status_code == 503
        assert "ENTRA_TENANT_ID" in r.json()["detail"]

    def test_the_azure_sdk_names_no_longer_configure_sign_in(self, auth_enabled, monkeypatch):
        """The old names are not read: AZURE_CLIENT_ID is the Azure SDK's, not ours."""
        from app.core.config import get_settings

        monkeypatch.setenv("ENTRA_TENANT_ID", "")
        monkeypatch.setenv("ENTRA_API_CLIENT_ID", "")
        monkeypatch.setenv("AZURE_TENANT_ID", TEST_TENANT)
        monkeypatch.setenv("AZURE_CLIENT_ID", TEST_CLIENT_ID)
        get_settings.cache_clear()
        r = client.get("/api/me", headers=auth_headers())
        assert r.status_code == 503
        assert "(ENTRA_TENANT_ID and ENTRA_API_CLIENT_ID not set)" in r.json()["detail"]

    def test_token_validated_once_per_request(self, auth_enabled, monkeypatch):
        calls = []
        real = security.validate_token

        def counting(token):
            calls.append(1)
            return real(token)

        monkeypatch.setattr(security, "validate_token", counting)
        r = probe_client.post("/twice", headers=auth_headers(roles=[Roles.VIEWER]))
        assert r.status_code == 200 and r.json() == {"same": True}
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Sign-in ON -- the dependencies
# ---------------------------------------------------------------------------


class TestRoleChecks:
    def test_holder_allowed(self, auth_enabled):
        r = probe_client.post("/stores", headers=auth_headers(roles=[Roles.STORES]))
        assert r.status_code == 200
        assert r.json() == {"oid": TEST_USER_OID, "authenticated": True}

    def test_other_role_refused(self, auth_enabled):
        r = probe_client.post("/stores", headers=auth_headers(roles=[Roles.VIEWER, Roles.REQUESTER]))
        assert r.status_code == 403
        assert r.json()["detail"]["requiredRoles"] == [Roles.STORES]

    def test_any_of_several(self, auth_enabled):
        assert probe_client.post("/ic-or-admin", headers=auth_headers(roles=[Roles.ADMIN])).status_code == 200
        assert probe_client.post("/ic-or-admin", headers=auth_headers(roles=[Roles.INVENTORY_CONTROLLER])).status_code == 200
        assert probe_client.post("/ic-or-admin", headers=auth_headers(roles=[Roles.STORES])).status_code == 403

    def test_no_token_is_401_not_403(self, auth_enabled):
        assert probe_client.post("/stores").status_code == 401

    def test_unknown_role_name_fails_at_import(self):
        with pytest.raises(ValueError, match="unknown role"):
            security.require_roles("Spares.Typo")
        with pytest.raises(ValueError):
            security.require_roles()

    def test_declared_roles_are_readable(self):
        dependency = security.require_roles(Roles.HOD, Roles.COMMERCIAL)
        assert dependency.__required_roles__ == {Roles.HOD, Roles.COMMERCIAL}


class TestReadRule:
    def test_any_role_reads(self, auth_enabled):
        for role in sorted(USER_ROLES):
            assert probe_client.get("/read", headers=auth_headers(roles=[role])).status_code == 200, role

    def test_no_role_refused(self, auth_enabled):
        assert probe_client.get("/read", headers=auth_headers(roles=[])).status_code == 403

    def test_application_token_refused(self, auth_enabled):
        headers = auth_headers(roles=[Roles.SAP_EVENTS_WRITE], application=True)
        assert probe_client.get("/read", headers=headers).status_code == 403

    def test_real_business_route_needs_sign_in(self, auth_enabled):
        # The read rule runs before the route itself, so no database is needed.
        assert client.get("/api/i8/register").status_code == 401
        assert client.get("/api/i8/register", headers=auth_headers(roles=[])).status_code == 403

    def test_health_stays_open(self, auth_enabled):
        assert client.get("/api/health").status_code == 200


class TestSapEvents:
    def test_off_means_open_even_with_sign_in_on(self, auth_enabled):
        assert probe_client.post("/sap").json() == {"caller": None}

    def test_entra_needs_a_token(self, sap_events_entra):
        assert probe_client.post("/sap").status_code == 401

    def test_entra_refuses_a_person(self, sap_events_entra):
        headers = auth_headers(roles=[Roles.ADMIN, Roles.SAP_EVENTS_WRITE])
        assert probe_client.post("/sap", headers=headers).status_code == 403

    def test_entra_refuses_an_application_without_the_role(self, sap_events_entra):
        assert probe_client.post("/sap", headers=auth_headers(application=True)).status_code == 403

    def test_entra_accepts_cpi(self, sap_events_entra):
        headers = auth_headers(roles=[Roles.SAP_EVENTS_WRITE], application=True, oid="55555555-5555-5555-5555-555555555555")
        r = probe_client.post("/sap", headers=headers)
        assert r.status_code == 200 and r.json() == {"caller": "55555555-5555-5555-5555-555555555555"}

    def test_real_routes_carry_it(self, sap_events_entra):
        assert client.post("/api/events/pr", json={}).status_code == 401
        assert client.post("/api/events/csv", content=b"x").status_code == 401


class TestActor:
    def test_token_user_replaces_the_header(self, auth_enabled):
        headers = {**auth_headers(roles=[Roles.REQUESTER]), "X-Actor-Id": "someone-else"}
        assert probe_client.post("/actor/shared", headers=headers).json() == {"actor": TEST_USER_OID}
        assert probe_client.post("/actor/i13", headers=headers).json() == {"actor": TEST_USER_OID}

    def test_no_token_no_actor(self, auth_enabled):
        assert probe_client.post("/actor/shared", headers={"X-Actor-Id": "alice"}).status_code == 401

    def test_sign_in_remembers_the_user(self, auth_enabled, monkeypatch):
        seen = []
        monkeypatch.setattr(user_directory, "remember", lambda user: seen.append(user))
        client.get("/api/me", headers=auth_headers(name="Ada Lovelace", email="ada@example.com"))
        assert [(u.oid, u.name, u.email) for u in seen] == [(TEST_USER_OID, "Ada Lovelace", "ada@example.com")]

    def test_applications_are_not_remembered(self, auth_enabled, monkeypatch):
        seen = []
        monkeypatch.setattr(user_directory, "remember", lambda user: seen.append(user))
        client.get("/api/me", headers=auth_headers(roles=[Roles.SAP_EVENTS_WRITE], application=True))
        assert seen == []


def test_role_catalogue_matches_the_app_registration():
    """The 16 values on spares-ai-api-<env> (Downloads/spares-ai-api-roles.json)."""
    assert ALL_ROLES == {
        "Spares.Viewer", "Spares.Requester", "Spares.Stores", "Spares.HOD", "Spares.Commercial",
        "Spares.InventoryController", "Spares.EndUser", "Spares.EngineeringManager",
        "Spares.CommercialManager", "Spares.WarehouseSupervisor", "Spares.InventoryControllerApprover",
        "Spares.CommercialHead", "Spares.EngineeringHead", "Spares.PlantHead", "Spares.Admin",
        "Sap.Events.Write",
    }
    assert Roles.SAP_EVENTS_WRITE not in USER_ROLES


# ---------------------------------------------------------------------------
# AZURE_CLIENT_ID belongs to the Azure SDK, not to sign-in
# ---------------------------------------------------------------------------

SERVICE_PRINCIPAL_VARIABLES = ("AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_FEDERATED_TOKEN_FILE")


class TestAzureSdkIdentityWarning:
    def test_quiet_when_unset(self):
        assert security.azure_sdk_identity_warning({}) is None
        assert security.azure_sdk_identity_warning({"AZURE_CLIENT_ID": "  "}) is None

    def test_warns_when_set_on_its_own(self):
        """Prod on 28 Oct 2026: the app registration's ID, with no secret beside it."""
        warning = security.azure_sdk_identity_warning({"AZURE_CLIENT_ID": TEST_CLIENT_ID, "AZURE_TENANT_ID": TEST_TENANT})
        assert warning is not None
        assert "managed identity" in warning
        assert "ENTRA_API_CLIENT_ID" in warning

    @pytest.mark.parametrize("name", SERVICE_PRINCIPAL_VARIABLES)
    def test_quiet_for_a_service_principal(self, name):
        assert security.azure_sdk_identity_warning({"AZURE_CLIENT_ID": TEST_CLIENT_ID, name: "set"}) is None

    def test_tenant_alone_is_harmless(self):
        assert security.azure_sdk_identity_warning({"AZURE_TENANT_ID": TEST_TENANT}) is None

    def test_reads_the_process_environment_by_default(self, monkeypatch):
        for name in SERVICE_PRINCIPAL_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AZURE_CLIENT_ID", TEST_CLIENT_ID)
        assert security.azure_sdk_identity_warning() is not None
        monkeypatch.delenv("AZURE_CLIENT_ID")
        assert security.azure_sdk_identity_warning() is None

    def test_logged_at_start_up(self, monkeypatch, caplog):
        from app.main import create_app

        # create_app() reconfigures logging, which would remove caplog's handler.
        monkeypatch.setattr("app.main.configure_logging", lambda settings: None)
        for name in SERVICE_PRINCIPAL_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AZURE_CLIENT_ID", TEST_CLIENT_ID)
        with caplog.at_level(logging.WARNING, logger="app.main"):
            create_app()
        assert any("AZURE_CLIENT_ID is set" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# The user directory
# ---------------------------------------------------------------------------


@pytest.fixture
def directory_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[AppUser.__table__])
    with sessionmaker(bind=engine)() as session:
        yield session


class TestUserDirectory:
    def test_first_sighting_recorded_once(self, directory_db):
        assert user_directory.record(directory_db, "oid-1", "Ada", "ada@example.com") is True
        assert user_directory.record(directory_db, "oid-1", "Ada", "ada@example.com") is False
        assert directory_db.scalar(select(func.count()).select_from(AppUser)) == 1

    def test_a_change_appends_and_the_latest_wins(self, directory_db):
        user_directory.record(directory_db, "oid-1", "Ada", "ada@example.com")
        user_directory.record(directory_db, "oid-1", "Ada King", "ada@example.com")
        assert directory_db.scalar(select(func.count()).select_from(AppUser)) == 2
        assert user_directory.names_for(directory_db, ["oid-1"]) == {"oid-1": ("Ada King", "ada@example.com")}

    def test_display_name(self, directory_db):
        user_directory.record(directory_db, "oid-1", "Ada", "ada@example.com")
        user_directory.record(directory_db, "oid-2", "", "bob@example.com")
        assert user_directory.display_name(directory_db, "oid-1") == "Ada <ada@example.com>"
        assert user_directory.display_name(directory_db, "oid-2") == "bob@example.com"
        assert user_directory.display_name(directory_db, "unknown-actor") == "unknown-actor"
        assert user_directory.names_for(directory_db, []) == {}

    def test_remember_is_a_no_op_without_a_database(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("DATABASE_URL", "")
        get_settings.cache_clear()
        submitted = []
        monkeypatch.setattr(user_directory._writer, "submit", lambda *a: submitted.append(a))
        try:
            user_directory.remember(CurrentUser(oid="oid-x", name="X"))
        finally:
            get_settings.cache_clear()
        assert submitted == []

    def test_remember_writes_once_per_user(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("DATABASE_URL", "sqlite://")
        get_settings.cache_clear()
        user_directory.forget_all()
        submitted = []
        monkeypatch.setattr(user_directory._writer, "submit", lambda fn, *a: submitted.append(a))
        try:
            user = CurrentUser(oid="oid-y", name="Y", email="y@example.com")
            user_directory.remember(user)
            user_directory.remember(user)
            user_directory.remember(CurrentUser(oid="oid-y", name="Y renamed", email="y@example.com"))
        finally:
            user_directory.forget_all()
            get_settings.cache_clear()
        assert submitted == [("oid-y", "Y", "y@example.com"), ("oid-y", "Y renamed", "y@example.com")]
