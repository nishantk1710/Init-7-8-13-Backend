"""AI-written reasons for Convert to OAR, with the standard wording as the fallback.

The same mechanism the Stock item / OAR recommendations use for their rationale
(``app.initiatives.i7.recommendations.rationale``), written again here because the Min-Max to OAR feature must not
import the forward ``recommendations`` package (an architecture test enforces it):

    RecommendationRow (already decided)
        -> prompt variables (this module)
        -> app.core.prompts.complete_with_prompt   (prompt registry + model registry + provider gateway)
        -> one explanatory paragraph + source

**The model explains; it does not decide.** The category, confidence and every figure are worked out by
``evaluate`` before this module runs. The prompt carries those facts and the model only rewords them; its text never
changes a decision.

**It never blocks or fails the run.** An unconfigured provider, a timeout, a malformed answer or a figure that was not
among the facts all end in the standard wording (``row.points`` and ``row.detail`` from ``evaluate``), never an exception. After a few
failures in a row the remaining rows skip the model, so a provider outage costs seconds, not one timeout per row.

``points_source`` says which it was: ``AI_GENERATED`` only when a real model wrote it. The stub provider
(``LLM_PROVIDER=stub``, the default) counts as the fallback: it answers but consulted no model.

Only Convert to OAR rows are written by the model. A Not Eligible row keeps the standard wording: nobody is asked to
act on it, so it is not worth a model call.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import date

from app.core.ai import AIError
from app.core.logging import get_logger
from app.core.prompts import complete_with_prompt
from app.initiatives.i7.reverse_conversion.service import RecommendationRow
from app.initiatives.i7.reverse_conversion.types import ReverseCategory

logger = get_logger(__name__)

SOURCE_AI = "AI_GENERATED"
SOURCE_FALLBACK = "DETERMINISTIC_FALLBACK"

PROMPT_ID = "i07_reverse_conversion_rationale"
_STUB_PROVIDER = "stub"  # matches app.integrations.ai.stub.StubProvider.name

LABELS = ("What we saw", "What it means", "What to do")
"""The labels of the standard wording; the model's paragraph has none."""

#: A paragraph longer than this is not the two or three sentences asked for; the row keeps the standard wording.
MAX_PARAGRAPH_CHARS = 700

#: Consecutive model failures after which the rest of the run uses the standard wording without calling the model.
MAX_CONSECUTIVE_FAILURES = 5

#: How often progress is logged, in rows.
PROGRESS_EVERY = 250

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _mon(day: date | None) -> str:
    return f"{day:%b %Y}" if day is not None else "unknown"


def _context(row: RecommendationRow) -> dict[str, str]:
    """Prompt variables, read from the decided row. Nothing is recomputed."""
    m = row.metrics
    saw, means, todo = (dict(row.points).get(label, "") for label in LABELS)
    lead = "not recorded" if row.lead_time_days is None else f"{float(row.lead_time_days):g} days"
    return {
        "material": row.material,
        "plant": row.plant,
        "criticality": (row.criticality or "not recorded").strip().title(),
        "confidence": row.confidence.value if row.confidence else "unknown",
        "reason": row.reason.value,
        "window": f"the 12 months ending {_mon(m.anchor)}",
        "issues": "unknown" if m.issues_latest_window is None else str(m.issues_latest_window),
        "active_months": "unknown" if m.months_with_use_latest_window is None else str(m.months_with_use_latest_window),
        "last_used": _mon(m.last_valid_month),
        "lead_time": lead,
        "saw": saw,
        "means": means,
        "todo": todo,
    }


def _parse(text: str) -> str | None:
    """The one paragraph out of the model's answer, or None when it is not that (empty, a list, headed, too long)."""
    paragraph = " ".join(text.split())
    if not paragraph or len(paragraph) > MAX_PARAGRAPH_CHARS:
        return None
    if paragraph[0] in "-*•#" or paragraph.lower().startswith(tuple(label.lower() + ":" for label in LABELS)):
        return None
    if "anchor" in paragraph.lower():
        return None
    return paragraph


def _only_given_figures(paragraph: str, allowed_text: str) -> bool:
    """True when every number in the answer also appears in the facts the model was given (a month's year included).

    A model that writes a figure it was not given is not trusted at all: the row keeps the standard wording."""
    allowed = set(_NUMBER.findall(allowed_text))
    return all(number in allowed for number in _NUMBER.findall(paragraph))


def _written(row: RecommendationRow) -> RecommendationRow:
    """``row`` with its reason written by the model, or unchanged (standard wording) on any failure."""
    context = _context(row)
    try:
        completion = complete_with_prompt(PROMPT_ID, task=PROMPT_ID, **context)
    except AIError as exc:
        logger.info("reverse conversion reason: model unavailable (%s), standard wording used", type(exc).__name__)
        raise
    text = (completion.text or "").strip()
    if not text or completion.provider == _STUB_PROVIDER:
        return row  # not a real model's answer: never labelled AI
    paragraph = _parse(text)
    if paragraph is None:
        logger.info("reverse conversion reason: answer is not one plain paragraph for %s/%s", row.material, row.plant)
        return row
    if not _only_given_figures(paragraph, " ".join(context.values())):
        logger.info("reverse conversion reason: answer holds a figure that was not given for %s/%s", row.material, row.plant)
        return row
    # The paragraph is the reason: it replaces the one-paragraph detail, and there are no labelled lines beside it.
    return replace(row, detail=paragraph[:1000], points=(), points_source=SOURCE_AI)


def explain_rows(rows: list[RecommendationRow], workers: int = 8) -> list[RecommendationRow]:
    """The same rows, in the same order, with each Convert to OAR reason written by the model where it can be.

    Never raises: every failure leaves that row on the standard wording."""
    targets = [i for i, row in enumerate(rows) if row.category is ReverseCategory.RECOMMENDED_FOR_OAR]
    if not targets:
        return rows
    out = list(rows)
    lock = threading.Lock()
    failures = 0  # consecutive
    counts = {SOURCE_AI: 0, SOURCE_FALLBACK: 0}

    def work(index: int) -> tuple[int, RecommendationRow]:
        nonlocal failures
        row = rows[index]
        with lock:
            if failures >= MAX_CONSECUTIVE_FAILURES:
                return index, row
        try:
            result = _written(row)
        except Exception as exc:  # noqa: BLE001 -- a reason is wording only; it must not fail the run
            if not isinstance(exc, AIError):
                logger.exception("reverse conversion reason: unexpected error, standard wording used")
            with lock:
                failures += 1
                if failures == MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "reverse conversion reason: %d model failures in a row; the rest use the standard wording",
                        failures,
                    )
            return index, row
        with lock:
            failures = 0
        return index, result

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="i7-reverse-reason") as pool:
        futures = [pool.submit(work, i) for i in targets]
        for done, future in enumerate(as_completed(futures), start=1):
            index, row = future.result()
            out[index] = row
            counts[row.points_source] = counts.get(row.points_source, 0) + 1
            if done % PROGRESS_EVERY == 0 or done == len(targets):
                logger.info(
                    "reverse conversion reasons: %d of %d done (%s)",
                    done,
                    len(targets),
                    ", ".join(f"{name} {count}" for name, count in sorted(counts.items())),
                )
    return out
