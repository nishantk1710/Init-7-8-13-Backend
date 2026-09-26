"""Architectural boundary tests.

These check properties of the *source tree*, not of any single function, because
that is where they can actually be broken: a well-meaning import of ``SapClient``
into a contract module would pass every behavioural test in the suite and
quietly weld the domain to one data source.

The leakage test mirrors the frontend's ``no-leakage.test.ts``, which fails the
build if scope field names or values appear outside the scope config. The same
rule needs enforcing on this side, or the two ends drift apart.
"""

import ast
from pathlib import Path

import pytest

I7_ROOT = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7"
CONTRACTS = I7_ROOT / "contracts"
POLICY = I7_ROOT / "policy"
ADAPTERS = I7_ROOT / "adapters"

# The single file allowed to name OAR field values -- the frontend has the same
# carve-out for its scope config.
OAR_CONFIG_FILE = POLICY / "oar.py"


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if path.name != "__pycache__")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# --- SAP independence -------------------------------------------------


@pytest.mark.parametrize("path", python_files(CONTRACTS), ids=lambda p: p.name)
def test_contracts_do_not_import_sap_integration(path):
    """The contract boundary is what makes the source swappable."""
    offending = {module for module in imported_modules(path) if "integrations" in module}
    assert not offending, f"{path.name} imports SAP integration code: {offending}"


@pytest.mark.parametrize("path", python_files(POLICY), ids=lambda p: p.name)
def test_policy_does_not_import_sap_integration(path):
    offending = {module for module in imported_modules(path) if "integrations" in module}
    assert not offending, f"{path.name} imports SAP integration code: {offending}"


@pytest.mark.parametrize("path", python_files(I7_ROOT), ids=lambda p: p.name)
def test_i7_does_not_import_other_initiatives(path):
    """I07, I08 and I13 must stay independently deletable."""
    offending = {
        module
        for module in imported_modules(path)
        if "initiatives.i8" in module or "initiatives.i13" in module
    }
    assert not offending, f"{path.name} imports another initiative: {offending}"


def test_policy_package_imports_standalone():
    """Importing policy first must work.

    Regression: ``contracts.recommendation`` once imported ``PolicyVersionRef``
    from ``policy.document`` while ``policy`` imported ``contracts`` -- a cycle.
    Every test happened to import ``contracts`` first, so the suite stayed green
    while ``import app.initiatives.i7.policy`` raised ImportError on its own.
    A subprocess is the only honest check: within one process the module cache
    hides the cycle after anything has imported either package.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import app.initiatives.i7.policy"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr


def test_contracts_package_imports_standalone():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import app.initiatives.i7.contracts"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr


def test_contracts_do_not_import_sqlalchemy():
    """Canonical contracts are domain types, not rows -- keeping them free of the
    ORM is what lets an adapter build one without a database present."""
    for path in python_files(CONTRACTS):
        assert not {m for m in imported_modules(path) if m.startswith("sqlalchemy")}, path.name


# --- EXTWG retirement -------------------------------------------------


def test_extwg_value_does_not_leak_into_i7_code():
    """"100" as an OAR identifier is retired. It may appear in prose, never as a
    configured value outside the OAR policy module."""
    for path in python_files(I7_ROOT):
        if path == OAR_CONFIG_FILE:
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and node.value == "100":
                pytest.fail(f'{path.name} contains the retired EXTWG value "100" as a literal')


def test_mrp_type_values_do_not_leak_outside_the_oar_policy():
    """"ND"/"PD" belong in one place. Scattering them is how a rule change
    becomes a code change -- the thing this phase exists to prevent."""
    for path in python_files(I7_ROOT):
        if path == OAR_CONFIG_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in {"ND", "PD"}:
                pytest.fail(f"{path.name} hardcodes MRP type {node.value!r}")


def test_the_oar_rule_lives_in_exactly_one_module():
    """MRP_TYPE in {ND, PD} -- the clarified rule; material_status no longer
    participates in the active predicate."""
    from app.initiatives.i7.policy.oar import current_oar_policy

    configured = {value for p in current_oar_policy().predicates for value in p.values}
    assert configured == {"ND", "PD"}


# --- Service levels are never guessed ---------------------------------


# --- The adapter transforms data; it makes no inventory decisions -------


RAW_TABLES = (
    "raw_mara",
    "raw_makt",
    "raw_marc",
    "raw_mseg",
    "raw_ekko",
    "raw_ekpo",
    "raw_ekbe",
    "raw_eket",
    "raw_mbew",
    "raw_cdhdr",
    "raw_cdpos",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of string constants that are docstrings -- prose, not executable SQL."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def test_only_the_adapter_reads_raw_tables():
    """Business logic goes through staging, never straight to the extract.

    Docstrings are excluded: a module explaining the architecture in prose is
    not the same as one querying the table.
    """
    for path in python_files(I7_ROOT):
        if path.is_relative_to(ADAPTERS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or id(node) in docstrings
            ):
                continue
            for table in RAW_TABLES:
                assert table not in node.value.lower(), (
                    f"{path.name} names the raw table {table} outside the adapter"
                )


def test_adapter_does_not_classify_or_calculate():
    """Phase 2 stages inputs. ADI, CV2, OAR verdicts and safety stock are later
    phases, and a calculation smuggled in here would silently fix a threshold
    into stored data where nobody could change it."""
    forbidden = (
        "adi",
        "cv_squared",
        "safety_stock_calc",
        "reorder_point_calc",
        "classify",
        "forecast",
        "pinball",
        "croston",
    )
    for path in python_files(ADAPTERS):
        source = path.read_text(encoding="utf-8").lower()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef):
                name = node.name.lower()
                for term in forbidden:
                    assert term not in name, f"{path.name}:{node.name} looks like Phase 3+ logic"


def test_staging_stores_no_oar_verdict():
    """OAR is decided by policy at read time, never frozen into a staged row."""
    from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

    for model in (StagedMaterial, StagedMaterialPlant):
        columns = {column.name for column in model.__table__.columns}
        assert "is_oar" not in columns
        assert "oar" not in columns
        assert "demand_pattern" not in columns


def test_no_service_level_percentage_is_hardcoded():
    """The Solution Design is explicit: do not assume 98% for critical."""
    suspicious = {0.85, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995}
    for path in python_files(I7_ROOT):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in suspicious:
                pytest.fail(
                    f"{path.name} contains {node.value}, which looks like an "
                    "assumed service level -- these must come from a signed matrix"
                )
