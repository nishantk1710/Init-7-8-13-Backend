"""The optional sentence a model writes around facts we already computed.

The deviation this implements, stated plainly
----------------------------------------------
Both FRSs end their assistant requirement with *"responses generated through the
provider-agnostic LLM layer."* Read literally, that puts the model in the path of
the numbers.

**Every number the assistant states is already a field we hold.** Repair status,
vendor, expected arrival and overdue flag sit on ``RepairLine``. Stock on hand,
months of cover, days since last movement, aging band and acquired-vs-plan sit on
``WatchMetric``. There is nothing to infer, so a model asked to produce them
would be inventing, not reasoning -- and a fabricated number with a decimal point
is indistinguishable from a real one to the person about to spend money on it.

So the facts are computed, and the model is only ever asked to write the sentence
around them. ``model_registry.py`` had already taken this position in code before
WS7 started: its ``i13_quantity_suggestion`` entry reads *"Explains an arithmetic
result. The arithmetic is not the model's job."*

**This is still a visible deviation from both FRSs and needs sign-off.** It is
off by default (``assistant_narrative_enabled``) so that nothing ships depending
on it before that conversation happens.

Failure discipline, borrowed from screen_material
--------------------------------------------------
Every failure mode lands on "no narrative" with the reason recorded, and the
deterministic sentence is what the requester sees. Nothing here can fail a turn:
the advice is already complete before the model is consulted, and losing a
paragraph of phrasing must never cost somebody their answer.

The one failure worth naming separately is **the stub provider**. It returns
obviously-synthetic placeholder text by design, and that text must never reach a
requester as though it were advice. It is detected and refused explicitly rather
than left to a reader to notice -- the same guard ``screen_material`` applies when
it says the stub "never" returns a readable verdict.

What the model is not allowed to change
----------------------------------------
The stored ``assessment`` is the deterministic record and is written before any
of this runs. The narrative is stored *beside* it, with the prompt id, prompt
version and deployment that produced it, so an AI-written sentence can always be
traced to the exact prompt behind it. "The model said so" is not acceptable
provenance on a programme that is human-gated and audited.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.ai import AIError
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.prompts import PromptError, complete_with_prompt
from app.integrations.ai.stub import STUB_MODEL

logger = get_logger(__name__)

#: The registry prompt ids. Both are routed in ``model_registry.py``.
I08_PROMPT = "reservation_assistant"

#: **Not** ``i13_quantity_suggestion``, which this used to point at.
#:
#: That prompt belongs to W7.4's suggestion engine
#: (``app/initiatives/i13/quantity_suggestion_reason.py``) and its placeholders
#: are that engine's inputs -- ``stock_on_hand``, ``suggested_quantity``,
#: ``cover_ceiling_months`` and four more. :func:`write` supplies ``headline``
#: and ``facts``, so every I13 narrative failed to render and degraded to
#: "prompt unavailable". The deterministic answer was served throughout, which
#: is why nothing looked broken -- but the I13 flow had no working narrative at
#: all, and the failure was silent because this layer is designed to be.
#:
#: Reusing one prompt for two callers with different variables could not have
#: worked. They are two prompts now.
I13_PROMPT = "i13_reservation_assistant"


@dataclass(frozen=True)
class Narrative:
    """A model-written sentence, and where it came from.

    ``text`` is ``None`` whenever no narrative was produced, and ``reason`` then
    says why. A null narrative beside a populated assessment is the honest
    record of a turn that used the deterministic sentence -- not a gap.
    """

    text: str | None
    reason: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    model: str | None = None

    @property
    def served(self) -> bool:
        return self.text is not None


def _skip(reason: str) -> Narrative:
    return Narrative(text=None, reason=reason)


def write(
    *,
    prompt_id: str,
    headline: str,
    facts: str,
    settings: Settings | None = None,
) -> Narrative:
    """Ask the model to phrase an answer we have already worked out.

    ``headline`` is the deterministic sentence of record and ``facts`` the
    numbers behind it. Both are handed over; nothing is left for the model to
    derive.
    """
    settings = settings or get_settings()

    if not settings.assistant_narrative_enabled:
        return _skip("narrative is disabled (assistant_narrative_enabled)")

    if not settings.llm_configured:
        return _skip(f"the {settings.llm_provider} provider is not configured")

    try:
        completion = complete_with_prompt(prompt_id, headline=headline, facts=facts)
    except PromptError as error:
        return _skip(f"prompt unavailable: {error}")
    except AIError as error:
        # Reported, never raised. The advice is already complete; a provider
        # outage must not cost the requester their answer.
        return _skip(f"model unavailable: {error}")

    text = (completion.text or "").strip()

    if not text:
        return _skip(f"the {completion.provider or 'configured'} provider returned nothing")

    # The stub is deterministic placeholder prose by design. It must never be
    # shown to a requester as advice, so it is refused by name rather than left
    # for somebody to spot.
    if completion.model == STUB_MODEL or text.startswith("[stub completion"):
        return _skip(
            "the stub provider was used -- it returns placeholder text, never "
            "advice. Configure LLM_PROVIDER to serve a narrative."
        )

    logger.info(
        "Narrative written by %s (%s v%s)",
        completion.model,
        completion.prompt_id,
        completion.prompt_version,
    )
    return Narrative(
        text=text,
        reason=None,
        prompt_id=completion.prompt_id,
        prompt_version=completion.prompt_version,
        model=completion.model,
    )
