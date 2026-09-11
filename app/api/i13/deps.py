"""Shared FastAPI dependencies for the Initiative 13 routes."""

from pathlib import Path

from app.core.config import get_settings


def get_data_dir() -> Path:
    """The CSV-backed SAP/platform dataset root (see ``Settings.i13_data_dir``)."""
    return Path(get_settings().i13_data_dir)
