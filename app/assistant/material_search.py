"""Find a material by number or by name, for the assistant's Material field.

The opener used to take a material number and nothing else. A coordinator
standing at the form usually knows the part ("the slurry pump impeller"), not
its ten digits, so this answers a typed fragment with the material+plant pairs
it could mean. Picking one fills both the material and the plant.

Read-only, and it decides nothing
---------------------------------
Nothing here writes, mints a session or calls a model. ``flow_hint`` is a
**hint**: it is worked out with the same two predicates the router uses
(:func:`app.assistant.router.route`) so the badge and the routing agree, but the
router still decides when the session opens. If they ever disagree, the router
is right and this is the bug.

Where the rows come from, and why there are several sources
------------------------------------------------------------
* **Descriptions** -- MAKT (``n_makt``, English first), then the ZMM065 aging
  reports. ZMM065 covers roughly four times as many repair materials as MAKT
  (measured for the I08 register), so a search over MAKT alone would miss most
  80-series parts by name.
* **Plants** -- MARC (``n_marc``) and MARD (``n_mard``), plus the plants the
  ZMM065 rows name. MARC has **no Gamsberg (1500) rows** in the July extract, so
  a MARC-only spine would never suggest a 1500 material at all.
* **Flow hint** -- MARC's MRP type only, exactly what the router reads. A pair
  known only from MARD or ZMM065 has no MRP type and so can only be I08 (by its
  number) or nothing, which is also what the router would say.

ZMM065 is a workbook report, not an extract table, and may not be loaded. A
missing report table is skipped, not an error: the search still works on MAKT.

Plants 1300 and 1500 only (``app/shared/plant_scope.py``), applied in SQL.

From memory when it can
-----------------------
The route passes the in-memory copy of these four sources
(:mod:`app.assistant.material_index`) once it has loaded, and the search then
asks the database nothing: on Azure SQL the plant lookup over the normalising
views took ~20 s per keystroke. Until the copy loads, the search reads the
database as below. Both paths apply the same rules and give the same answer;
``tests/assistant/test_material_search.py`` runs every case through both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.assistant.material_index import MaterialIndex
from app.assistant.router import Flow
from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.shared.material_scope import MaterialScope, classify_material_scope
from app.shared.material_scope.deletion import not_deleted_predicate
from app.shared.plant_scope import PLANT_NAMES, sql_predicate

logger = get_logger(__name__)

#: Shortest query answered. One character matches a large part of the catalogue
#: and tells the person nothing.
MIN_QUERY_LENGTH = 2

DEFAULT_LIMIT = 10
MAX_LIMIT = 25

#: How many distinct materials are carried forward to the plant lookup. Bounds
#: the IN list and the rows moved; far more than a person will read.
_CANDIDATE_MATERIALS = 60

#: Words of a name query that are matched. More than this is a sentence, not a
#: part name, and every extra word is another LIKE over the description.
_MAX_WORDS = 5

#: What the monthly ZMM065 criticality reports add up to: one row per
#: material-plant, with the description the report states.
_REPORT_TABLE = "criticality_current"

_WORD = re.compile(r"[A-Z0-9]+")


@dataclass(frozen=True)
class MaterialMatch:
    """One material at one plant that the typed text could mean."""

    material_id: str
    """Normalised -- leading zeros stripped, the form the router compares."""

    plant: str
    description: str | None
    """``None`` where neither MAKT nor ZMM065 names it. Found by number only."""

    flow_hint: Flow
    mrp_type: str | None

    @property
    def plant_name(self) -> str | None:
        return PLANT_NAMES.get(self.plant)


@dataclass(frozen=True)
class _Query:
    """A typed fragment, read as either a number prefix or a set of words."""

    raw: str
    number_prefix: str | None
    words: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return self.number_prefix is None and not self.words


def parse_query(raw: str | None) -> _Query:
    """Read a fragment as a material-number prefix (all digits) or as words.

    Digits are a number prefix with leading zeros stripped, because the
    normalised views hold the short form -- '000000008000' and '8000' are the
    same prefix. Anything else is split into words of two or more letters or
    digits; punctuation is dropped, which also keeps LIKE wildcards out of the
    pattern.
    """
    text_ = (raw or "").strip()
    if len(text_) < MIN_QUERY_LENGTH:
        return _Query(raw=text_, number_prefix=None, words=())

    compact = re.sub(r"[\s-]", "", text_)
    if compact.isdigit():
        prefix = compact.lstrip("0")
        return _Query(raw=text_, number_prefix=prefix or None, words=())

    words = tuple(
        dict.fromkeys(w for w in _WORD.findall(text_.upper()) if len(w) >= 2)
    )[:_MAX_WORDS]
    return _Query(raw=text_, number_prefix=None, words=words)


def search(
    db: Session,
    raw_query: str | None,
    *,
    limit: int = DEFAULT_LIMIT,
    i8_config: I8Settings | None = None,
    index: MaterialIndex | None = None,
) -> list[MaterialMatch]:
    """The material+plant pairs a typed fragment could mean, best first.

    With ``index`` the sources are read from memory and ``db`` is not touched.
    """
    query = parse_query(raw_query)
    if query.empty:
        return []
    limit = max(1, min(limit, MAX_LIMIT))
    i8_config = i8_config or get_i8_settings()

    if index is not None:
        descriptions, report_pairs = _candidates_from_index(index, query)
    else:
        descriptions, report_pairs = _candidates(db, query)
    if query.number_prefix is not None:
        by_prefix = (
            index.with_prefix(query.number_prefix, _CANDIDATE_MATERIALS)
            if index is not None
            else _materials_by_prefix(db, query.number_prefix)
        )
        for material in by_prefix:
            descriptions.setdefault(material, None)

    # Exact number first, then by number, so the carried-forward set is stable.
    materials = sorted(descriptions, key=lambda m: (m != query.number_prefix, m))
    materials = materials[:_CANDIDATE_MATERIALS]
    if not materials:
        return []

    mrp_types, pairs = (
        _plants_from_index(index, materials) if index is not None else _plants(db, materials)
    )
    pairs |= {pair for pair in report_pairs if pair[0] in descriptions}

    matches = []
    for material, plant in pairs:
        if material not in descriptions:
            continue
        mrp_type = mrp_types.get((material, plant))
        matches.append(
            MaterialMatch(
                material_id=material,
                plant=plant,
                description=descriptions[material],
                flow_hint=_flow_hint(material, mrp_type, i8_config),
                mrp_type=mrp_type,
            )
        )

    matches.sort(key=lambda m: _rank(m, query))
    return matches[:limit]


def _flow_hint(material: str, mrp_type: str | None, i8_config: I8Settings) -> Flow:
    """The router's precedence, over the same two predicates. I08 wins."""
    if is_eighty_series(material, i8_config):
        return Flow.I08
    if classify_material_scope(mrp_type) is MaterialScope.OAR:
        return Flow.I13
    return Flow.NONE


def _rank(match: MaterialMatch, query: _Query) -> tuple:
    """Parts the assistant can talk about first, then the closest match.

    A name like "pump" matches consumables as well as repairables, and a
    consumable gets no conversation at all -- so in-scope rows go first rather
    than being pushed off the list by things that would open nothing.
    """
    description = (match.description or "").upper()
    exact_number = match.material_id == query.number_prefix
    leads_with_first_word = bool(query.words) and description.startswith(query.words[0])
    return (
        match.flow_hint is Flow.NONE,
        not exact_number,
        not leads_with_first_word,
        description == "",
        match.material_id,
        match.plant,
    )


# --- the queries -----------------------------------------------------------


def _description_filter(
    column: str, query: _Query, key_column: str, *, key_may_be_padded: bool = False
) -> tuple[str, dict]:
    """WHERE fragment for one text source: every word, or the number prefix."""
    if query.number_prefix is not None:
        # The normalised views hold the short form, so a plain prefix works. A
        # report may carry the padded form, so there it is matched loosely in
        # SQL and exactly in Python after normalising.
        pattern = f"%{query.number_prefix}%" if key_may_be_padded else f"{query.number_prefix}%"
        return f"{key_column} LIKE :number_like", {"number_like": pattern}
    clauses = [f"UPPER({column}) LIKE :w{i}" for i in range(len(query.words))]
    params = {f"w{i}": f"%{word}%" for i, word in enumerate(query.words)}
    return " AND ".join(clauses), params


def _matches_prefix(material: str, query: _Query) -> bool:
    return query.number_prefix is None or material.startswith(query.number_prefix)


def _candidates(
    db: Session, query: _Query
) -> tuple[dict[str, str | None], set[tuple[str, str]]]:
    """Materials whose description (or number) matches, and the plants ZMM065
    names for them. MAKT's English text wins over ZMM065's."""
    descriptions: dict[str, str | None] = {}
    english: set[str] = set()

    where, params = _description_filter("material_description", query, "material")
    rows = db.execute(
        text(
            f"""
            SELECT material, language_key, material_description
            FROM n_makt
            WHERE material <> '' AND material_description <> '' AND {where}
            """
        ),
        params,
    ).fetchall()
    for row in rows:
        material = normalise(row.material)
        if not material or not _matches_prefix(material, query):
            continue
        is_english = (row.language_key or "").strip().upper() in ("E", "EN")
        if material in english:
            continue
        if is_english or material not in descriptions:
            descriptions[material] = (row.material_description or "").strip() or None
        if is_english:
            english.add(material)

    report_pairs: set[tuple[str, str]] = set()
    where, params = _description_filter(
        "description", query, "material", key_may_be_padded=True
    )
    try:
        rows = db.execute(
            text(
                f"""
                SELECT material, plant, description
                FROM {_REPORT_TABLE}
                WHERE material <> '' AND {sql_predicate("plant")} AND {where}
                """
            ),
            params,
        ).fetchall()
    except DBAPIError as error:
        # No criticality report loaded yet. The search still works on MAKT.
        logger.info("material search: %s not readable (%s)", _REPORT_TABLE, error.__class__.__name__)
        db.rollback()
        rows = []
    for row in rows:
        material = normalise(row.material)
        plant = (row.plant or "").strip()
        if not material or not _matches_prefix(material, query):
            continue
        if descriptions.get(material) is None:
            descriptions[material] = (row.description or "").strip() or None
        if plant:
            report_pairs.add((material, plant))

    return descriptions, report_pairs


def _materials_by_prefix(db: Session, prefix: str) -> list[str]:
    """Materials with no description anywhere can still be found by number."""
    found: list[str] = []
    for table in ("n_marc", "n_mard"):
        rows = db.execute(
            text(
                f"""
                SELECT DISTINCT material
                FROM {table}
                WHERE material LIKE :prefix AND plant <> '' AND {sql_predicate("plant")}
                """
            ),
            {"prefix": f"{prefix}%"},
        ).fetchmany(_CANDIDATE_MATERIALS)
        found.extend(normalise(row.material) or "" for row in rows)
    return [m for m in dict.fromkeys(found) if m]


def _plants(
    db: Session, materials: list[str]
) -> tuple[dict[tuple[str, str], str | None], set[tuple[str, str]]]:
    """Every in-scope plant each material is held at, and MARC's MRP type."""
    mrp_types: dict[tuple[str, str], str | None] = {}
    pairs: set[tuple[str, str]] = set()

    marc = db.execute(
        text(
            f"""
            SELECT material, plant, mrp_type
            FROM n_marc mc
            WHERE material IN :materials AND plant <> '' AND {sql_predicate("plant")}
              {not_deleted_predicate("mc")}
            """
        ).bindparams(bindparam("materials", expanding=True)),
        {"materials": materials},
    ).fetchall()
    for row in marc:
        key = (normalise(row.material) or "", (row.plant or "").strip())
        # Blank stays "" -- a blank DISMM is OAR; None means no MARC row.
        mrp_types[key] = (row.mrp_type or "").strip()
        pairs.add(key)

    mard = db.execute(
        text(
            f"""
            SELECT DISTINCT material, plant
            FROM n_mard
            WHERE material IN :materials AND plant <> '' AND {sql_predicate("plant")}
            """
        ).bindparams(bindparam("materials", expanding=True)),
        {"materials": materials},
    ).fetchall()
    for row in mard:
        pairs.add((normalise(row.material) or "", (row.plant or "").strip()))

    return mrp_types, {pair for pair in pairs if pair[0] and pair[1]}


# --- the same, from memory ---------------------------------------------------------


def _candidates_from_index(
    index: MaterialIndex, query: _Query
) -> tuple[dict[str, str | None], set[tuple[str, str]]]:
    """:func:`_candidates` over the loaded copy: the same filters, the same
    precedence (MAKT English text, then any MAKT text, then ZMM065)."""
    descriptions: dict[str, str | None] = {}
    english: set[str] = set()

    for material, texts in index.makt.items():
        if query.number_prefix is not None:
            if not material.startswith(query.number_prefix):
                continue
            matching = texts
        else:
            matching = [t for t in texts if all(word in t.upper for word in query.words)]
        for t in matching:
            if material in english:
                break
            if t.english or material not in descriptions:
                descriptions[material] = t.text.strip() or None
            if t.english:
                english.add(material)

    report_pairs: set[tuple[str, str]] = set()
    for row in index.report:
        if query.number_prefix is not None:
            if not row.material.startswith(query.number_prefix):
                continue
        elif not all(word in row.upper for word in query.words):
            continue
        if descriptions.get(row.material) is None:
            descriptions[row.material] = (row.description or "").strip() or None
        if row.plant:
            report_pairs.add((row.material, row.plant))

    return descriptions, report_pairs


def _plants_from_index(
    index: MaterialIndex, materials: list[str]
) -> tuple[dict[tuple[str, str], str | None], set[tuple[str, str]]]:
    """:func:`_plants` over the loaded copy."""
    mrp_types: dict[tuple[str, str], str | None] = {}
    pairs: set[tuple[str, str]] = set()
    for material in materials:
        for plant, mrp_type in index.marc.get(material, ()):
            mrp_types[(material, plant)] = mrp_type
            pairs.add((material, plant))
        for plant in index.mard.get(material, ()):
            pairs.add((material, plant))
    return mrp_types, pairs
