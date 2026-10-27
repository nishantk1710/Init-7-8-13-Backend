"""The rows an I08 build reads, staged before any per-row function runs.

Why
---
Every I08 view computes its keys with ``sap_key()`` and filters its plants with
``in_scope_plant()`` -- scalar functions, which SQL Server calls once per row,
serially, and cannot push a predicate through. Reading ``v_ekpo where matnr
like '80%'`` therefore calls ``sap_key`` on every EKPO row before it can drop
one. On the production extract (measured 12 Oct 2026) that is 805,336 EKPO
lines and 2,578,082 in-scope MSEG rows, of which the build uses about 20,000
and 44,000. The build ran past its 10-minute statement limit, and the 805,336
candidate rows it pulled into the web worker took it past 600 MB.

How
---
Two steps per view, both inside the build's own connection (temp tables):

1. **Narrow the source with plain predicates.** ``SELECT * INTO #i8b_<table>
   FROM n_<table> WHERE ...`` -- only column comparisons, ``LIKE`` and the
   inline form of the same key normalisation ``sap_key`` performs. No scalar
   function, so SQL Server scans set-wise and discards early. Every filter is a
   SUPERSET of what the build's queries select (``material LIKE '%80%'`` for
   ``matnr LIKE '80%'``, ``item_category = '3'`` for ``pstyp == '3'``, the
   purchase orders of the staged EKPO lines for ``ebeln in :documents``), so no
   row a query would have read is left out.
2. **Run the view's own SQL over the narrowed rows.** ``SELECT * INTO
   #i8s_v_<table> FROM (<the view body, reading #i8b_<table>>)``. The view
   scripts are read and rendered exactly as ``views.ensure_views`` renders them,
   so every rule -- the key normalisation, the date and number casts, the
   ``'0'`` item mapped to NULL -- still lives in one place. The helper calls in
   them are written inline (:data:`INLINE_HELPERS`): called outside a view,
   they need a permission the API's database identity does not have.

The build's queries then read ``#i8s_v_<table>`` where they read ``v_<table>``,
and return what they returned. ``candidates_scanned`` -- the EKPO lines the
register looked at -- is counted over the unfiltered source with the same plain
predicates, so the reported figure does not change either.

``tests/test_i8_staged_build.py`` builds a seeded tenant both ways and compares
every line, universe row and statistic.

Who stages what
---------------
:func:`staged_sources` serves the snapshot build (the register and universe),
narrowing on repair item category and 80-series materials.

:func:`coding_sources` serves the FR-2 coding screen, which has a different
question and therefore a different narrowing: it wants the lines whose free
text mentions repair, whatever the material. It was left reading the views
directly when the snapshot build was staged, and paid exactly the price this
module exists to avoid -- six unfiltered passes over 805,336 EKPO lines, each
calling ``sap_key`` per row. Measured against nonprod on 13-Oct-2026 the
endpoint never completed: 504 at the gateway's 240s, and still not cached
after six idle minutes.

Postgres (local only now) reads the views directly: it inlines the helpers and
has expression indexes for the material key -- see ``views.INDEXES``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import MSSQL
from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.views import OPTIONAL_REPORT_TABLES, SQL_DIR, render
from app.shared.plant_scope import sql_literals
from app.shared.sql_lists import LIKE_ESCAPE, like_literal

logger = get_logger(__name__)

#: The views a snapshot build reads. Not v_ekbe since 19-Oct-2026: the
#: register follows SAP's "Delivery completed" flag, not goods receipts.
#: v_resb and v_eban since 27-Oct-2026: the open repair PRs (requisitions.py).
#: v_mchb and v_mbew since 27-Oct-2026: stock per condition (universe.py) --
#: only the one I8_STOCK_CONDITION_SOURCE names is staged; see CONDITION_STOCK.
VIEWS: tuple[str, ...] = (
    "v_ekpo", "v_ekko", "v_eket", "v_mseg", "v_lfa1",
    "v_mara", "v_makt", "v_marc", "v_mard", "v_zmm065",
    "v_resb", "v_eban", "v_mchb", "v_mbew",
)

#: I8_STOCK_CONDITION_SOURCE -> (view, normalised table, its plant column).
#: MBEW's plant is its valuation area (valuation is at plant level here).
CONDITION_STOCK: dict[str, tuple[str, str, str]] = {
    "MCHB": ("v_mchb", "n_mchb", "plant"),
    "MBEW": ("v_mbew", "n_mbew", "valuation_area"),
}


@dataclass(frozen=True)
class Sources:
    """Where a build reads each view from, and the EKPO candidate count."""

    views: dict[str, str] = field(default_factory=lambda: {view: view for view in VIEWS})
    candidates_scanned: int | None = None
    """Lines the unfiltered ``v_ekpo`` holds, when the build reads a narrowed
    copy; ``None`` means the build reads ``v_ekpo`` itself and counts it."""


DIRECT = Sources()


# --- plain predicates over the n_* columns ----------------------------------


def _key(column: str) -> str:
    """``sap_key(column)`` written inline: trimmed, leading zeros stripped, '' as
    NULL. The same expression as the function body in 00_functions.mssql.sql."""
    return f"NULLIF(SUBSTRING(TRIM({column}), PATINDEX('%[^0]%', TRIM({column}) + '.'), 4000), '')"


def _in_scope(column: str = "plant") -> str:
    """``in_scope_plant(column)`` written inline."""
    return f"TRIM(COALESCE({column}, '')) IN ({sql_literals()})"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _series(column: str, cfg: I8Settings) -> str:
    """A superset of ``sap_key(column) LIKE '<prefix>%'``: the prefix anywhere in
    the raw value, whatever padding or spacing surrounds it."""
    prefixes = cfg.series_prefix_list
    if not prefixes:
        return "1 = 0"
    likes = " OR ".join(
        f"{column} LIKE {_literal('%' + like_literal(p) + '%')} ESCAPE '{LIKE_ESCAPE}'" for p in prefixes
    )
    return f"({likes})"


# --- the view bodies, as ensure_views renders them --------------------------

_FILES: dict[str, str] = {
    "v_mara": "10_v_mara.sql", "v_makt": "11_v_makt.sql", "v_marc": "12_v_marc.sql",
    "v_mard": "13_v_mard.sql", "v_ekko": "20_v_ekko.sql", "v_ekpo": "21_v_ekpo.sql",
    "v_eket": "22_v_eket.sql", "v_ekbe": "23_v_ekbe.sql", "v_eban": "24_v_eban.sql",
    "v_mseg": "30_v_mseg.sql", "v_lfa1": "31_v_lfa1.sql", "v_zmm065": "32_v_zmm065.sql",
    "v_mbew": "33_v_mbew.sql", "v_resb": "25_v_resb.sql", "v_mchb": "34_v_mchb.sql",
}
_HEAD = re.compile(r"(?is)\bCREATE\s+OR\s+ALTER\s+VIEW\s+\w+\s+AS\s")


def _body(view: str) -> str:
    """The SELECT a view is defined as, rendered for SQL Server."""
    [statement] = render((SQL_DIR / _FILES[view]).read_text(encoding="utf-8"), mssql=True)
    match = _HEAD.search(statement)
    if match is None:  # pragma: no cover - the scripts are ours
        raise ValueError(f"I08: no view definition in {_FILES[view]}")
    return statement[match.end():]


# The helpers' bodies, written inline. Called directly -- as a view body run
# outside its view calls them -- a scalar function needs EXECUTE permission,
# which the API's database identity does not have; inside a view, ownership
# chaining covers it, so the views never needed it. (Azure, 12 Oct 2026: "The
# EXECUTE permission was denied on the object 'sap_date'".) Inline, the staged
# copies need nothing beyond SELECT, and SQL Server evaluates them set-wise
# instead of one call per row. ``{v}`` is the argument, cast as the function's
# nvarchar(4000) parameter casts it. Pinned to the bodies in
# 00_functions.mssql.sql by tests/test_i8_staged_sql.py.
INLINE_HELPERS: dict[str, str] = {
    "sap_key": "nullif(substring(trim({v}), patindex('%[^0]%', trim({v}) + '.'), 4000), '')",
    "sap_date": (
        "case when trim({v}) like '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        " then try_convert(date, trim({v}), 23) end"
    ),
    "sap_num": "try_cast(trim({v}) as decimal(38, 6))",
}
_HELPER_CALL = re.compile(r"(?i)\bdbo\.(" + "|".join(INLINE_HELPERS) + r")\((\w+)\)")
_IN_SCOPE_CALL = re.compile(r"(?i)\bdbo\.in_scope_plant\((\w+)\)\s*=\s*1")
_ANY_CALL = re.compile(r"(?i)\bdbo\.\w+\(")


def inline_helpers(body: str) -> str:
    """``body`` with every I08 helper call replaced by the helper's own SQL."""
    body = _IN_SCOPE_CALL.sub(lambda m: f"({_in_scope(m.group(1))})", body)

    def inline(match: re.Match) -> str:
        argument = f"CAST({match.group(2)} AS NVARCHAR(4000))"
        return "(" + INLINE_HELPERS[match.group(1).lower()].format(v=argument) + ")"

    body = _HELPER_CALL.sub(inline, body)
    if _ANY_CALL.search(body):
        raise ValueError("I08: a helper call could not be inlined -- only a bare column argument is supported")
    return body


def _over(body: str, sources: dict[str, str]) -> str:
    """``body`` reading ``sources[table]`` wherever it reads ``table``."""
    for table, staged in sources.items():
        body = re.sub(rf"(?i)\b{table}\b", staged, body)
    return body


# --- staging ---------------------------------------------------------------


def _materialise(
    db: Session, view: str, sources: dict[str, str], name: str | None = None
) -> str:
    """``#i8s_<view>``: the view's own SELECT over the narrowed sources.

    ``name`` overrides the staged table's name, so one build can hold two
    differently-narrowed copies of the same view -- which the coding screen
    does: one narrowed to repair language, one to 80-series materials.
    """
    staged = name or f"#i8s_{view}"
    db.execute(text(f"DROP TABLE IF EXISTS {staged}"))
    # No bind parameters anywhere in staging: a parameterised statement runs in
    # sp_executesql, which drops a temp table it creates when it returns.
    db.execute(text(f"SELECT * INTO {staged} FROM (\n{_over(inline_helpers(_body(view)), sources)}\n) AS v"))
    return staged


def _narrow(db: Session, table: str, source: str, where: str, name: str | None = None) -> str:
    narrowed = name or f"#i8b_{table}"
    db.execute(text(f"DROP TABLE IF EXISTS {narrowed}"))
    db.execute(text(f"SELECT * INTO {narrowed} FROM {source} WHERE {where}"))
    return narrowed


def _empty_report(db: Session, table: str) -> str:
    """An empty stand-in for a report table that was never loaded."""
    narrowed = f"#i8b_{table}"
    columns = ", ".join(f"CAST(NULL AS NVARCHAR(200)) AS {c}" for c in OPTIONAL_REPORT_TABLES[table])
    db.execute(text(f"DROP TABLE IF EXISTS {narrowed}"))
    db.execute(text(f"SELECT {columns} INTO {narrowed} WHERE 1 = 0"))
    return narrowed


def _count(db: Session, table: str) -> int:
    return int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def _stage(db: Session, cfg: I8Settings) -> Sources:
    import time

    from app.shared.optional_tables import existing

    started = time.monotonic()

    views: dict[str, str] = {}
    sizes: dict[str, int] = {}

    def stage(view: str, narrowed: dict[str, str]) -> None:
        views[view] = _materialise(db, view, narrowed)
        sizes[view] = _count(db, views[view])

    # EKPO first: its purchase orders and materials narrow everything else.
    # Kept: repair lines (the register) and 80-series lines (new purchases, and
    # the universe's EKPO slice), each in scope -- the view filters plant too.
    repair = _literal(cfg.repair_item_category)
    stage("v_ekpo", {"n_ekpo": _narrow(
        db, "ekpo", "n_ekpo",
        f"{_in_scope()} AND (item_category = {repair} OR {_series('material', cfg)})",
    )})
    db.execute(text("DROP TABLE IF EXISTS #i8s_docs"))
    db.execute(text(f"SELECT DISTINCT ebeln INTO #i8s_docs FROM {views['v_ekpo']} WHERE ebeln IS NOT NULL"))
    db.execute(text("CREATE CLUSTERED INDEX ix_i8s_docs ON #i8s_docs (ebeln)"))
    db.execute(text("DROP TABLE IF EXISTS #i8s_mats"))
    db.execute(text(f"SELECT DISTINCT matnr INTO #i8s_mats FROM {views['v_ekpo']} WHERE matnr IS NOT NULL"))
    db.execute(text("CREATE CLUSTERED INDEX ix_i8s_mats ON #i8s_mats (matnr)"))

    on_staged_po = "IN (SELECT ebeln FROM #i8s_docs)"
    stage("v_ekko", {"n_ekko": _narrow(db, "ekko", "n_ekko", f"{_key('purchasing_document')} {on_staged_po}")})
    stage("v_eket", {"n_eket": _narrow(db, "eket", "n_eket", f"{_key('purchasing_document')} {on_staged_po}")})
    # Open repair PRs (27-Oct-2026). RESB: the repair orders' lines for the
    # faulty unit -- the movement type is a setting written in as a literal
    # (no bind parameter, see _materialise) and compared exactly, as the view
    # passes the column through unchanged; 6,271 of the in-scope rows on the
    # 15-Oct extract are 543 lines on 80-series parts. EBAN: the in-scope
    # 80-series PR lines (12,369 then), which holds every PR those lines name
    # and, later, the new-buy PRs too. requisitions.py applies the exact rules.
    # Since 27-Oct-2026 every other 80-series line WITH TEXT is kept too: the
    # job's reservation line names the assistant session there
    # (session_jobs.py); the movement-type rule stays in Python. Staged ahead
    # of MSEG, whose narrowing reads these reservation numbers.
    stage("v_resb", {"n_resb": _narrow(
        db, "resb", "n_resb",
        f"{_in_scope()} AND {_series('material', cfg)} "
        f"AND (movement_type = {_literal(cfg.repair_reservation_movement_type)} "
        f"OR COALESCE(text, '') <> '')",
    )})
    # MSEG: the staged POs' movements, plus (27-Oct-2026) those posted against a
    # staged reservation line with text -- the job's 261/262 (session_jobs.py).
    # Readers filter by PO or by reservation, so neither sees the other's rows.
    stage("v_mseg", {"n_mseg": _narrow(
        db, "mseg", "n_mseg",
        f"{_in_scope()} AND ({_key('purchase_order')} {on_staged_po} "
        f"OR {_key('reservation')} IN (SELECT rsnum FROM {views['v_resb']} WHERE sgtxt IS NOT NULL))",
    )})
    # Vendor names are only ever looked up for an EKKO header's supplier.
    stage("v_lfa1", {"n_lfa1": _narrow(
        db, "lfa1", "n_lfa1", f"{_key('vendor')} IN (SELECT lifnr FROM {views['v_ekko']} WHERE lifnr IS NOT NULL)"
    )})

    # Materials: the 80-series universe, plus -- for MARC's lead times -- every
    # material on a staged PO line, which includes non-80 repair lines.
    stage("v_mara", {"n_mara": _narrow(db, "mara", "n_mara", _series("material", cfg))})
    stage("v_makt", {"n_makt": _narrow(db, "makt", "n_makt", _series("material", cfg))})
    stage("v_mard", {"n_mard": _narrow(db, "mard", "n_mard", f"{_in_scope()} AND {_series('material', cfg)}")})
    stage("v_marc", {"n_marc": _narrow(
        db, "marc", "n_marc",
        f"{_in_scope()} AND ({_series('material', cfg)} OR {_key('material')} IN (SELECT matnr FROM #i8s_mats))",
    )})
    present = set(existing(db, OPTIONAL_REPORT_TABLES))
    stage("v_zmm065", {
        table: (
            _narrow(db, table, table, f"{_in_scope()} AND {_series('material', cfg)}")
            if table in present
            else _empty_report(db, table)
        )
        for table in OPTIONAL_REPORT_TABLES
    })

    stage("v_eban", {"n_eban": _narrow(db, "eban", "n_eban", f"{_in_scope()} AND {_series('material', cfg)}")})

    # Stock per condition (27-Oct-2026): only the configured source, in scope
    # and 80-series, as MARD is narrowed. 3,432 in-scope 80-series MCHB rows on
    # the 15-Oct extract. Named #i8b_/#i8s_, apart from the coding screen's
    # #i8cb_mbew / #i8cs_v_mbew, so the two never drop each other's copy.
    view, table, plant = CONDITION_STOCK[cfg.stock_condition_source]
    stage(view, {table: _narrow(
        db, table.removeprefix("n_"), table, f"{_in_scope(plant)} AND {_series('material', cfg)}"
    )})

    # What the register reports as scanned: v_ekpo's rows with a PO and an
    # item, counted over the unfiltered source with the view's own predicates.
    scanned = int(db.execute(text(
        f"SELECT COUNT(*) FROM n_ekpo WHERE {_in_scope()} "
        f"AND {_key('purchasing_document')} IS NOT NULL AND NULLIF(item, '') IS NOT NULL"
    )).scalar_one())

    logger.info(
        "I08 snapshot: staged sources in %.1fs (%s; %d EKPO lines in scope)",
        time.monotonic() - started,
        ", ".join(f"{view.removeprefix('v_')} {n}" for view, n in sizes.items()),
        scanned,
    )
    return Sources(views=views, candidates_scanned=scanned)


@contextmanager
def staged_sources(db: Session, cfg: I8Settings, *, enabled: bool = True) -> Iterator[Sources]:
    """The sources a build reads from, staged on SQL Server for the build's life.

    Everything is created on ``db``'s connection, so ``db`` must not commit in
    between (a commit could hand the session another connection, without them).
    """
    if not enabled or db.get_bind().dialect.name != MSSQL:
        yield DIRECT
        return
    sources = _stage(db, cfg)
    try:
        yield sources
    finally:
        for table in [*sources.views.values(), "#i8s_docs", "#i8s_mats"]:
            db.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for name in ("ekpo", "ekko", "eket", "mseg", "lfa1", "mara", "makt", "mard", "marc",
                     "criticality_current", "resb", "eban", "mchb", "mbew"):
            db.execute(text(f"DROP TABLE IF EXISTS #i8b_{name}"))


_VIEW_NAME = re.compile(r"\bv_[a-z0-9]+\b")


def reading(sql: str, sources: Sources | None) -> str:
    """``sql`` with each view it names replaced by where ``sources`` stages it."""
    if sources is None or sources is DIRECT:
        return sql
    return _VIEW_NAME.sub(lambda m: sources.views.get(m.group(0), m.group(0)), sql)


# --- FR-2: the coding screen's sources -------------------------------------

#: source -> (view, normalised table, the raw column the view reads as text).
#: The view's own text column is ``nullif(<raw>, '')``, so a non-empty LIKE
#: over the raw column matches exactly what a LIKE over the view's would.
CODING_SOURCES: dict[str, tuple[str, str, str]] = {
    "PO": ("v_ekpo", "n_ekpo", "short_text"),
    "PR": ("v_eban", "n_eban", "short_text"),
    "MOVEMENT": ("v_mseg", "n_mseg", "text"),
}


@dataclass(frozen=True)
class CodingSources:
    """Where the coding screen reads, and how much staging kept.

    Two narrowings, because the screen asks two different questions:

    ``candidates``
        the lines whose free text mentions repair -- any material, since a
        miscoded material is the thing being looked for.
    ``twins``
        the 80-series lines, for the corroboration check: "is this exact text
        already carried by a material somebody DID code repairable?".

    ``kept`` and ``scanned`` are the selectivity of the narrowing, per staged
    table. They are reported in the screen's ``meta`` rather than only logged,
    because the whole approach rests on ``kept`` being a small fraction of
    ``scanned`` -- and if a keyword list is ever widened until it is not, the
    number that shows it is on screen.
    """

    candidates: Sources = DIRECT
    twins: Sources = DIRECT
    kept: dict[str, int] = field(default_factory=dict)
    scanned: dict[str, int] = field(default_factory=dict)


DIRECT_CODING = CodingSources()


def _mentions(column: str, cfg: I8Settings) -> str:
    """A superset of the screen's keyword match, over the raw text column.

    The screen matches ``upper(<view text>) LIKE '%STEM%' ESCAPE`` for each
    configured stem (``I8_CODING_LANGUAGE``), ORed. The view's text column is
    ``nullif(<raw>, '')``, and an empty string matches no ``'%stem%'`` pattern
    for a non-empty stem, so the same LIKE over the raw column keeps every row
    the screen's own predicate would -- and the staged copy is then filtered
    again by that predicate, so an over-wide superset costs time, never
    correctness.

    No stems configured means the screen matches nothing, and staging keeps
    nothing rather than everything.
    """
    stems = cfg.repair_language_list
    if not stems:
        return "1 = 0"
    likes = " OR ".join(
        f"UPPER({column}) LIKE {_literal('%' + like_literal(stem.upper()) + '%')} ESCAPE '{LIKE_ESCAPE}'"
        for stem in stems
    )
    return f"({likes})"


def plain_text_total(source: str) -> str | None:
    """``COUNT(*)`` of a source's lines carrying any free text, without the view.

    The view's text column is ``nullif(<raw>, '')`` and its only other
    predicate is the plant scope, so counting the normalised table with both
    written inline returns exactly what counting the view returns -- set-wise,
    with no per-row function call.

    This exists because the denominator CANNOT come from the staged candidate
    copies: those hold only lines whose text mentions repair, so counting them
    would make every source read as 100%.
    """
    spec = CODING_SOURCES.get(source)
    if spec is None:
        return None
    _, table, column = spec
    return (
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE {_in_scope()} AND NULLIF({column}, '') IS NOT NULL"
    )


def _stage_coding(db: Session, cfg: I8Settings, wanted: Sequence[str]) -> CodingSources:
    import time

    started = time.monotonic()
    kept: dict[str, int] = {}
    scanned: dict[str, int] = {}
    candidate_views: dict[str, str] = {}

    # One narrowing per source the run actually reads. A source nobody asked
    # for is not staged: MOVEMENT is off by default and staging it would cost
    # a pass over 2.5M MSEG rows to answer a question nobody asked.
    for source in wanted:
        spec = CODING_SOURCES.get(source)
        if spec is None:
            continue
        view, table, column = spec
        narrowed = _narrow(
            db,
            table.removeprefix("n_"),
            table,
            f"{_in_scope()} AND {_mentions(column, cfg)}",
            name=f"#i8cb_{table.removeprefix('n_')}",
        )
        candidate_views[view] = _materialise(
            db, view, {table: narrowed}, name=f"#i8cs_{view}"
        )
        kept[view] = _count(db, candidate_views[view])
        # What it was narrowed FROM, with the view's own scope predicate: the
        # denominator the keyword filter has to be selective against.
        scanned[view] = int(
            db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {_in_scope()}")).scalar_one()
        )

    # The twin side: 80-series PO lines, whatever their text. Narrowed exactly
    # as the snapshot build narrows its own 80-series slice.
    twin_narrowed = _narrow(
        db, "ekpo", "n_ekpo",
        f"{_in_scope()} AND {_series('material', cfg)}",
        name="#i8ct_ekpo",
    )
    twin_views = {"v_ekpo": _materialise(db, "v_ekpo", {"n_ekpo": twin_narrowed}, name="#i8ct_v_ekpo")}
    kept["v_ekpo (twins)"] = _count(db, twin_views["v_ekpo"])

    logger.info(
        "I08 coding: staged sources in %.1fs (%s)",
        time.monotonic() - started,
        ", ".join(f"{view} {n} of {scanned.get(view, n)}" for view, n in kept.items()),
    )
    return CodingSources(
        candidates=Sources(views=candidate_views),
        twins=Sources(views=twin_views),
        kept=kept,
        scanned=scanned,
    )


@contextmanager
def coding_sources(
    db: Session, cfg: I8Settings, *, wanted: Sequence[str], enabled: bool = True
) -> Iterator[CodingSources]:
    """The sources the FR-2 coding screen reads from, staged for its life.

    Everything is created on ``db``'s connection, so ``db`` must not commit
    while this is open -- a commit could hand the session another connection,
    without them. The screen's own writes (stored verdicts) happen after it
    closes, for that reason.
    """
    if not enabled or db.get_bind().dialect.name != MSSQL:
        yield DIRECT_CODING
        return
    sources = _stage_coding(db, cfg, wanted)
    try:
        yield sources
    finally:
        staged = [
            *sources.candidates.views.values(),
            *sources.twins.views.values(),
            "#i8ct_ekpo",
            *(f"#i8cb_{table.removeprefix('n_')}" for _, table, _ in CODING_SOURCES.values()),
        ]
        for table in staged:
            db.execute(text(f"DROP TABLE IF EXISTS {table}"))


# --- FR-2: the New Item Price's valuations ---------------------------------


@contextmanager
def valuation_sources(
    db: Session, materials: Sequence[str], *, enabled: bool = True
) -> Iterator[Sources]:
    """``v_mbew`` narrowed to ``materials``, staged for the price lookup's life.

    Reading ``v_mbew where matnr in (...)`` directly would call ``sap_key`` and
    ``in_scope_plant`` on every MBEW row before discarding nearly all of them --
    the cost that took the coding screen to 504 on 13-Oct-2026. The narrowing
    is the same predicate written plainly, so it keeps exactly the rows the
    lookup asks for.

    The materials go into an indexed temp table and the narrowing semi-joins
    to it, as the snapshot build narrows MSEG to ``#i8s_docs``. Until
    19-Oct-2026 they were an ``IN`` list of ~113 literals instead, and SQL
    Server re-evaluated the key expression once per literal per row: the
    lookup took 698-966 s on nonprod, and the screen 504'd at the gateway.

    The materials are written in as literals, not bound: a parameterised
    statement runs in ``sp_executesql``, which drops a temp table it creates
    when it returns. They are normalised keys the screen read from the views,
    and :func:`_literal` escapes them all the same.

    Opened inside :func:`coding_sources`, on the same connection and under the
    same rule: ``db`` must not commit while it is open.
    """
    if not enabled or not materials or db.get_bind().dialect.name != MSSQL:
        yield DIRECT
        return
    wanted = "#i8cm_mats"
    db.execute(text(f"DROP TABLE IF EXISTS {wanted}"))
    # DATABASE_DEFAULT: a temp table otherwise takes tempdb's collation, and
    # comparing it with n_mbew's could fail on a collation conflict.
    db.execute(text(f"CREATE TABLE {wanted} (matnr NVARCHAR(100) COLLATE DATABASE_DEFAULT NOT NULL)"))
    listed = sorted(set(materials))
    for start in range(0, len(listed), 1000):  # SQL Server's limit on one VALUES list
        rows = ", ".join(f"({_literal(m)})" for m in listed[start:start + 1000])
        db.execute(text(f"INSERT INTO {wanted} (matnr) VALUES {rows}"))
    db.execute(text(f"CREATE CLUSTERED INDEX ix_i8cm_mats ON {wanted} (matnr)"))
    narrowed = _narrow(
        db, "mbew", "n_mbew",
        f"{_in_scope('valuation_area')} AND {_key('material')} IN (SELECT matnr FROM {wanted})",
        name="#i8cb_mbew",
    )
    staged = _materialise(db, "v_mbew", {"n_mbew": narrowed}, name="#i8cs_v_mbew")
    try:
        yield Sources(views={"v_mbew": staged})
    finally:
        for table in (staged, narrowed, wanted):
            db.execute(text(f"DROP TABLE IF EXISTS {table}"))
