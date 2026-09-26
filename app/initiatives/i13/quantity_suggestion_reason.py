"""W7.4: phrasing a quantity suggestion's reason for a human reader.

The one place W7.4 touches the AI layer, and it touches it for prose only.
``quantity_suggestion.py`` produces the number and a deterministic sentence;
this module may replace the *sentence*, never the number. That split is the
same reasoning that keeps W6.2's stitching deterministic: a purchase figure
has to be reproducible and auditable, and "the model said 40" is not a
defensible provenance for a spares order.

Everything here degrades to the deterministic sentence:

* **No real provider configured.** The default provider is the deterministic
  stub (see ``app/integrations/ai/stub.py``), whose output is openly
  synthetic placeholder text. Persisting that as the reason a requester
  reads would be worse than the plain sentence, so the stub is skipped
  rather than called.
* **The provider fails, times out or is misconfigured.** Caught, and the
  deterministic sentence stands. A suggestion is still a suggestion when the
  model is down.
* **No suggestion was made.** A declined result's sentence explains a policy
  decision (history below the minimum, ceiling not configured) and needs no
  model. The engine's own wording is already the exact answer.

Provenance travels with the result (``ReasonPhrasing.source``, ``prompt_id``,
``prompt_version``, ``model``) and is persisted, because this codebase's
prompt registry exists precisely so an AI-written sentence can be traced back
to the prompt version and model that produced it months later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.ai import AIError
from app.core.config import Settings, get_settings
from app.core.prompts import PromptError, complete_with_prompt
from app.initiatives.i13.quantity_suggestion import QuantitySuggestion

logger = logging.getLogger(__name__)

PROMPT_ID = "i13_quantity_suggestion"

DETERMINISTIC = "DETERMINISTIC"
MODEL = "MODEL"


@dataclass(frozen=True)
class ReasonPhrasing:
    """One suggestion's reason text, and where the words came from."""

    text: str
    source: str
    """``DETERMINISTIC`` or ``MODEL``. Never inferred from the text later --
    a reader must be able to tell at a glance whether a model wrote it."""
    prompt_id: str | None = None
    prompt_version: int | None = None
    model: str | None = None


def _deterministic(suggestion: QuantitySuggestion) -> ReasonPhrasing:
    return ReasonPhrasing(text=suggestion.reason_text, source=DETERMINISTIC)


def _model_available(settings: Settings) -> bool:
    """Whether a *real* provider is plugged in. The stub is configured and
    healthy by design, so ``llm_configured`` alone is not the question."""
    return (settings.llm_provider or "stub").strip().lower() != "stub" and settings.llm_configured


def phrase_reason(suggestion: QuantitySuggestion, *, settings: Settings | None = None) -> ReasonPhrasing:
    """The reason text to show and persist for one suggestion.

    Returns the engine's deterministic sentence unless a real provider is
    configured and answers, in which case the model's phrasing of the same
    figures is used. The caller does not need to handle failure -- there
    isn't a failure path out of here.
    """
    settings = settings or get_settings()

    if not suggestion.has_suggestion or not _model_available(settings):
        return _deterministic(suggestion)

    try:
        completion = complete_with_prompt(
            PROMPT_ID,
            material=suggestion.material,
            plant=suggestion.plant,
            requested_quantity=suggestion.requested_quantity,
            suggested_quantity=suggestion.suggested_quantity,
            direction=suggestion.direction.value,
            reason_code=suggestion.reason_code.value,
            average_monthly_consumption=suggestion.average_monthly_consumption,
            lookback_months=suggestion.lookback_months,
            stock_on_hand=suggestion.stock_on_hand,
            open_po_quantity=suggestion.open_po_quantity,
            plan_window_months=suggestion.plan_window_months,
            cover_ceiling_months=suggestion.cover_ceiling_months,
            resulting_cover_months=suggestion.resulting_cover_months,
        )
    except (AIError, PromptError) as exc:
        # Deliberately broad in effect, narrow in type: every provider error
        # in this codebase subclasses AIError, and a prompt that has drifted
        # from its caller raises PromptError. Neither is a reason to fail a
        # suggestion whose arithmetic is already complete.
        logger.warning("i13 quantity-suggestion reason fell back to the deterministic sentence: %s", exc)
        return _deterministic(suggestion)

    text = (completion.text or "").strip()
    if not text:
        logger.warning("i13 quantity-suggestion reason: provider returned empty text; using the deterministic sentence")
        return _deterministic(suggestion)

    return ReasonPhrasing(
        text=text,
        source=MODEL,
        prompt_id=completion.prompt_id,
        prompt_version=completion.prompt_version,
        model=completion.model,
    )
