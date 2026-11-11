"""``GET /api/users/names`` -- display names for Entra object ids.

Every write records its author as the Entra object id from the token, so the screens that show
who did something hold only that id. This turns a batch of ids into names and e-mails from the
directory the sign-in layer keeps (``app.core.user_directory``: one row per person, written at
sign-in). An id that is not a known user -- never signed in, or a placeholder from before sign-in
existed -- is simply absent from the answer, and the screen keeps showing the id. Nothing here
reaches Entra.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.user_directory import names_for
from app.integrations.notifications.resolver import is_entra_object_id

router = APIRouter(tags=["auth"])

#: The most ids one request resolves: a screen asks for what is on it, never for the directory.
MAX_IDS = 500


class PersonName(BaseModel):
    oid: str
    name: str = Field(description="Display name at the last sign-in; empty when the token carried none.")
    email: str = Field(description="E-mail at the last sign-in; empty when the token carried none.")


class UserNamesResponse(BaseModel):
    names: dict[str, PersonName] = Field(description="Keyed by object id. Ids the directory does not know are absent.")


@router.get("/users/names", response_model=UserNamesResponse, summary="Display names for Entra object ids")
def user_names(
    db: Annotated[Session, Depends(get_db)],
    oid: Annotated[list[str] | None, Query(description="Entra object ids; repeat the parameter for each.")] = None,
) -> UserNamesResponse:
    wanted = sorted({value for value in oid or [] if is_entra_object_id(value)})[:MAX_IDS]
    found = names_for(db, wanted)
    return UserNamesResponse(
        names={
            key: PersonName(oid=key, name=name, email=email)
            for key, (name, email) in found.items()
            if name or email
        }
    )
