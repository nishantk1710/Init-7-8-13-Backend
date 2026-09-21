"""Command line entry point for building the serving layer.

    python -m app.serving --material-plant
    python -m app.serving --all

Reads only ``odata_*``, so it needs the database and nothing else -- no SAP, no
storage. That means it can be re-run at any time to pick up a corrected
normalise rule without re-fetching a single row.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.serving import material_plant

logger = get_logger(__name__)

BUILDERS = {
    "material-plant": material_plant,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.serving",
        description="Build the serving tables from the raw OData layer.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="build every serving table")
    group.add_argument(
        "--material-plant",
        action="store_true",
        help="build the material-plant dimension",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())

    chosen = (
        list(BUILDERS.values())
        if args.all
        else [material_plant]
    )

    failures = 0
    print(f"{'TABLE':<20} {'STATUS':<10} {'ROWS':>10}  {'SECONDS':>8}")
    for builder in chosen:
        result = builder.build()
        status = "ok" if result.ok else "FAILED"
        failures += 0 if result.ok else 1
        print(
            f"{builder.TARGET:<20} {status:<10} {result.rows:>10,}  "
            f"{result.seconds:>8.1f}"
        )
        for warning in result.warnings:
            print(f"  warning: {warning}")
        if result.error:
            print(f"  {result.error}")

    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
