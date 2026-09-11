"""Seed the local database from the July SAP extracts.

Replaces the synthetic ``data-generator`` output as the development data source.

    python -m app.seed --list          what would be loaded, and from where
    python -m app.seed --all           load everything
    python -m app.seed --table marc    load one table
    python -m app.seed --all --force   reload even if nothing changed

Requires ``DATABASE_URL`` and ``STORAGE_URL``. See README, "Seeding".
"""

from app.seed.loader import TableResult, load_all, load_table
from app.seed.manifest import EXTRACTS, ExtractSpec, spec_for

__all__ = ["EXTRACTS", "ExtractSpec", "TableResult", "load_all", "load_table", "spec_for"]
