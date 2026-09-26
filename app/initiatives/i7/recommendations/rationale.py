"""AI-generated recommendation rationale, with a deterministic fallback.

    BuiltRecommendation
        -> structured prompt context (this module)
        -> app.core.prompts.complete_with_prompt (the existing prompt/model
           registry, itself calling app.core.ai.get_llm() -- the existing
           provider-agnostic gateway)
        -> rationale text + source

No provider SDK is imported here, and none is imported anywhere in
``app.initiatives.i7`` -- only ``app.core.ai``/``app.core.prompts``, exactly
as every other AI consumer in this codebase (I08, I13, the reservation
assistant) already does. Foundry/OpenAI-specific code stays inside
``app/integrations/ai/``.

**Rationale generation never blocks or fails recommendation calculation.**
Every failure mode -- unconfigured provider, timeout, provider error,
malformed response -- is caught here and answered with the existing
deterministic factors (``explanation.build_factors``), never a raised
exception and never an empty rationale. SS/ROP/Max are computed and
persisted by Phase 5/6 long before this module runs; a Foundry outage
cannot touch them.

**The LLM explains; it does not decide.** The prompt is built entirely from
values Phase 3/4/5/6 already computed (see ``_context_for``) -- demand
class, criticality, current/recommended SS/ROP/Max, OAR evidence. The model
is never asked to compute, adjust or approve a number, and its text is never
written back into any calculated field. ``rationale_source`` records whether
the text actually came from a configured provider (``AI_GENERATED``) or the
deterministic template (``DETERMINISTIC_FALLBACK``) -- the stub provider
(``LLM_PROVIDER=stub``, the default) counts as the fallback case: it always
succeeds but never used a real model, and claiming otherwise would be
exactly the "the model said so" provenance gap ``app.core.prompts`` exists
to prevent.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.ai import AIError
from app.core.prompts import complete_with_prompt

if TYPE_CHECKING:
    from app.initiatives.i7.recommendations.builder import BuiltRecommendation

logger = logging.getLogger(__name__)

RATIONALE_SOURCE_AI = "AI_GENERATED"
RATIONALE_SOURCE_FALLBACK = "DETERMINISTIC_FALLBACK"

_NORMAL_PROMPT_ID = "i07_recommendation_rationale"
_OAR_PROMPT_ID = "i07_oar_conversion_rationale"

_STUB_PROVIDER_NAME = "stub"
"""Matches ``app.integrations.ai.stub.StubProvider.name``. The stub always
"succeeds" (it never raises), but it did not consult a real model -- its
output must never be labelled AI_GENERATED."""


@dataclass(frozen=True)
class RationaleResult:
    text: str
    source: str
    """``AI_GENERATED`` or ``DETERMINISTIC_FALLBACK``."""

    model: str | None = None
    """The resolved deployment/model name, when the source is AI_GENERATED.
    ``None`` for a fallback -- there is no model to attribute the text to."""

    provider: str | None = None


def _fmt(value: Decimal | int | None) -> str:
    return "unknown" if value is None else str(value)


def _deterministic_text(factors: tuple) -> str:
    """The existing template-based rationale, unchanged -- one line per
    factor, exactly what ``factors_text`` already stored before this slice."""
    if not factors:
        return "No supporting factors were computed for this recommendation."
    return " ".join(factor.detail for factor in factors)


def _normal_context(built: "BuiltRecommendation") -> dict[str, str]:
    """Prompt variables for ``i07_recommendation_rationale`` -- the same
    fields ``v1.md`` already names, read straight from the already-built
    recommendation, never recomputed."""
    return {
        "material": built.sap_material_number,
        "plant": built.sap_plant_code,
        "criticality": built.criticality or "unknown",
        "demand_pattern": built.demand_class or "unknown",
        "annual_consumption": _fmt(built.forecast_rate),
        "lead_time_days": _fmt(built.lead_time_days),
        "current_rop": _fmt(built.current_rop),
        "current_safety_stock": _fmt(built.current_safety_stock),
        "recommended_rop": _fmt(built.recommended_rop),
        "recommended_safety_stock": _fmt(built.recommended_safety_stock),
    }


def _oar_context(built: "BuiltRecommendation") -> dict[str, str]:
    """Prompt variables for ``i07_oar_conversion_rationale`` -- OAR-specific
    evidence only: consumption evidence, demand class as a supporting
    signal, and the similarity-weighted estimate. Never eligibility or the
    estimate formula itself -- those are already decided by
    ``conversion.py``/``oar/estimate.py`` before this module ever runs."""
    consumption_count_12m = built.conversion.consumption_count_12m if built.conversion else None
    return {
        "material": built.sap_material_number,
        "plant": built.sap_plant_code,
        "demand_class": built.demand_class or "UNCLASSIFIED",
        "consumption_count_12m": _fmt(consumption_count_12m),
        "neighbour_count": _fmt(built.oar_neighbour_count),
        "best_similarity": _fmt(built.oar_best_similarity),
        "current_rop": _fmt(built.current_rop),
        "current_safety_stock": _fmt(built.current_safety_stock),
        "recommended_rop": _fmt(built.recommended_rop),
        "recommended_safety_stock": _fmt(built.recommended_safety_stock),
    }


def generate_rationale(built: "BuiltRecommendation") -> RationaleResult:
    """The rationale for one recommendation, AI-generated when possible.

    Called once at recommendation-generation time (see
    ``service.generate_recommendations``), never per API GET -- an approver
    reading the same recommendation twice must see the same rationale, not a
    new model call each time.
    """
    is_oar = bool(built.is_oar)
    factors = built.factors

    if built.recommended_rop is None and built.recommended_safety_stock is None:
        # Nothing was actually computed for this material-plant (e.g. no
        # Phase 6 run has evaluated it yet -- see
        # ``builder.deferred_recommendation``). There is nothing for a model
        # to explain that the deterministic factors do not already say, so
        # this skips the AI call entirely rather than prompting a model with
        # an all-"unknown" context.
        return RationaleResult(
            text=_deterministic_text(factors), source=RATIONALE_SOURCE_FALLBACK
        )

    prompt_id = _OAR_PROMPT_ID if is_oar else _NORMAL_PROMPT_ID
    context_fn = _oar_context if is_oar else _normal_context

    try:
        context = context_fn(built)
        completion = complete_with_prompt(
            prompt_id,
            task="i07_recommendation_rationale",
            **context,
        )
    except AIError as exc:
        logger.info(
            "recommendation rationale: %s unavailable (%s), using deterministic fallback",
            prompt_id,
            type(exc).__name__,
        )
        return RationaleResult(
            text=_deterministic_text(factors), source=RATIONALE_SOURCE_FALLBACK
        )
    except Exception:
        # Any other failure -- a missing context key, an unexpected row
        # shape -- must still not block the recommendation. Logged with a
        # stack trace since this is not an expected AI-layer failure mode.
        logger.exception(
            "recommendation rationale: unexpected error building %s, using "
            "deterministic fallback",
            prompt_id,
        )
        return RationaleResult(
            text=_deterministic_text(factors), source=RATIONALE_SOURCE_FALLBACK
        )

    text = (completion.text or "").strip()
    if not text or completion.provider == _STUB_PROVIDER_NAME:
        # An empty/whitespace-only response is not a usable rationale, and
        # the stub is not a real model -- both fall back rather than being
        # mislabelled as AI_GENERATED.
        return RationaleResult(
            text=_deterministic_text(factors), source=RATIONALE_SOURCE_FALLBACK
        )

    return RationaleResult(
        text=text,
        source=RATIONALE_SOURCE_AI,
        model=completion.model,
        provider=completion.provider,
    )
