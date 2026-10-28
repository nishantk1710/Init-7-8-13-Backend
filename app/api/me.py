"""``GET /api/me`` -- who the API thinks is calling, and with which roles.

The web app reads it for the profile page and to enable only the buttons the
user's roles allow (a convenience: the API enforces every rule itself). Mounted
without the read rule, so a signed-in user who has not been given a role yet
still gets an answer -- an empty role list -- rather than a 403 the web app
cannot explain.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.core.security import CurrentUser, current_user

router = APIRouter(tags=["auth"])


class MeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    oid: str = Field(description="Entra object ID -- what the API records as the author of a write.")
    name: str
    email: str
    roles: list[str] = Field(description="The Spares.* roles the token carries (every role while sign-in is off).")
    auth_enabled: bool = Field(alias="authEnabled", description="False while AUTH_ENABLED is off on the API.")
    is_application: bool = Field(alias="isApplication")


@router.get("/me", response_model=MeResponse, summary="The signed-in user and their roles")
def me(user: CurrentUser = Depends(current_user)) -> MeResponse:
    return MeResponse(
        oid=user.oid,
        name=user.name,
        email=user.email,
        roles=sorted(user.roles),
        auth_enabled=user.authenticated,
        is_application=user.is_application,
    )
