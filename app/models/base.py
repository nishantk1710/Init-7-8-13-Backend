"""Declarative base for every ORM model.

Two rules for anything that subclasses ``Base``:

1. **Portable constructs only.** No ``JSONB``, no ``ARRAY``, no ``ON CONFLICT``.
   The database is Azure SQL. Models stay on portable constructs anyway: those are not
   guaranteed to be the same engine. Portable models keep that swap small;
   dialect-specific ones turn it into a rewrite.
2. **Import the model somewhere Alembic sees it**, or autogenerate will not
   notice it exists. ``app/models/__init__.py`` is that place.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint and index names. Without this SQLAlchemy lets the
# database invent names, Alembic autogenerate cannot match them between runs,
# and every migration diff churns on constraints nobody changed.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
