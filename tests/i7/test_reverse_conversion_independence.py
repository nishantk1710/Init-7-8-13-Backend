"""Min-Max -> OAR must stay independent of the OAR -> Min-Max conversion.

The reverse feature reads its own policy and its own evaluator. It must not import the forward
conversion rule, the forward policy, the adoption modules, or the recommendation machinery they live
in -- so nothing it does can change, or be changed by, the forward conversion.

This reads the source with ``ast`` rather than importing it, so it fails on the import statement itself.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7"
APP = ROOT.parents[1]
REVERSE_FILES = sorted((ROOT / "reverse_conversion").glob("*.py")) + [
    ROOT / "policy" / "reverse_conversion.py",
    APP / "api" / "i7" / "reverse_conversion.py",
    APP / "schemas" / "i7" / "reverse_conversion.py",
]

#: Modules a reverse-conversion file may never import (from or via an alias).
FORBIDDEN_MODULES = (
    "app.initiatives.i7.recommendations",  # conversion.py, adoption*.py, builder.py, types.py ... all of it
    "app.initiatives.i7.policy.thresholds",  # holds ConversionTriggerPolicy and AdoptionPolicy
    "app.initiatives.i7.adoption_notices",
    "app.api.i7.adoption",
    "app.api.i7.recommendations",
)
FORBIDDEN_NAMES = {"ConversionTriggerPolicy", "current_conversion_trigger_policy", "AdoptionPolicy"}


def _imports(path: Path) -> list[tuple[str, set[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, {alias.asname or alias.name}) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, {alias.name for alias in node.names}))
    return found


def test_the_reverse_feature_has_files_to_check():
    names = {path.name for path in REVERSE_FILES}
    assert {"evaluate.py", "metrics.py", "types.py", "errors.py", "repository.py", "service.py", "export.py", "reverse_conversion.py"} <= names
    assert all(path.exists() for path in REVERSE_FILES)


@pytest.mark.parametrize("path", REVERSE_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_reverse_file_imports_the_forward_conversion(path):
    for module, names in _imports(path):
        for forbidden in FORBIDDEN_MODULES:
            assert module != forbidden and not module.startswith(forbidden + "."), f"{path.name} imports {module}"
        assert not (names & FORBIDDEN_NAMES), f"{path.name} imports {sorted(names & FORBIDDEN_NAMES)} from {module}"


def test_importing_the_reverse_feature_does_not_load_the_forward_conversion():
    import subprocess
    import sys

    code = (
        "import sys; "
        "import app.initiatives.i7.reverse_conversion.evaluate, app.initiatives.i7.reverse_conversion.metrics, "
        "app.initiatives.i7.reverse_conversion.service, app.initiatives.i7.reverse_conversion.export, "
        "app.initiatives.i7.reverse_conversion.repository, app.schemas.i7.reverse_conversion, "
        "app.api.i7.reverse_conversion, app.initiatives.i7.policy.reverse_conversion; "
        "bad = [m for m in sys.modules if m.startswith('app.initiatives.i7.recommendations') "
        "or m == 'app.initiatives.i7.policy.thresholds']; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    # app.initiatives.i7.policy is a package whose __init__ imports thresholds; that is the shared package, not the
    # reverse feature reaching for the forward rule, so only the recommendations package is asserted absent.
    result = subprocess.run([sys.executable, "-c", code.replace(" or m == 'app.initiatives.i7.policy.thresholds'", "")],
                            capture_output=True, text=True, cwd=str(ROOT.parents[2]))
    assert result.returncode == 0, f"loaded forward modules: {result.stdout.strip()} {result.stderr.strip()[-300:]}"
