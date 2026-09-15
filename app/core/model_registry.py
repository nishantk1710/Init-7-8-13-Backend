"""Which model each job uses.

W1.5 asks for a *model and prompt registry*. ``prompts.py`` is the prompt half;
this is the model half.

VZI has two deployments -- ``gpt-4o`` and ``gpt-4o-mini`` -- and the difference
between them is cost, not correctness, so the choice belongs in configuration
rather than scattered through call sites. Two concrete cases from the FRSs:

* **I07 rationale** runs once per recommendation over a few hundred materials.
  Short, formulaic, and the reader is checking the numbers rather than admiring
  the prose. ``gpt-4o-mini``.
* **I08 coding-candidate screening** reads free text on 5,225 text-only PO lines
  looking for repair language that nobody coded as 80-series. Genuine language
  judgement on messy input, and a false negative means a missed repairable.
  ``gpt-4o``.

Each task resolves through ``model_for()``, so retuning is an environment
change. Nothing here imports a provider -- these are deployment *names*, handed
to whichever adapter is plugged in.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class ModelRoute:
    """One job, and the class of model it should use."""

    task: str
    tier: str
    """``fast`` or ``capable`` -- an intent, not a deployment name.

    Written as an intent so the mapping survives a renamed deployment: when the
    model changes, the tier stays true and only the setting moves.
    """
    why: str


# Every task that reaches a model. Adding one here rather than passing a
# deployment name at the call site keeps the cost decision reviewable.
ROUTES: dict[str, ModelRoute] = {
    "i07_recommendation_rationale": ModelRoute(
        task="i07_recommendation_rationale",
        tier="fast",
        why="Short, formulaic, high volume. The reader checks the numbers, not the prose.",
    ),
    "i08_coding_candidate": ModelRoute(
        task="i08_coding_candidate",
        tier="capable",
        why=(
            "Language judgement over 5,225 messy free-text PO lines. A false "
            "negative is a repairable spare bought new."
        ),
    ),
    "i13_quantity_suggestion": ModelRoute(
        task="i13_quantity_suggestion",
        tier="fast",
        why="Explains an arithmetic result. The arithmetic is not the model's job.",
    ),
    "reservation_assistant": ModelRoute(
        task="reservation_assistant",
        tier="capable",
        why="Interactive, user-facing, and answers shape a purchasing decision.",
    ),
}


def model_for(task: str, settings: Settings | None = None) -> str:
    """The deployment name for a task.

    Unknown tasks fall back to the default deployment rather than raising: a new
    caller should work, and get the safe-but-costlier model, instead of failing
    in production over a missing registry entry. The fallback is logged by the
    provider through the model name it reports.
    """
    settings = settings or get_settings()
    route = ROUTES.get(task)

    if route is None or route.tier == "capable":
        return settings.foundry_deployment or settings.llm_model

    return (
        settings.foundry_deployment_fast
        or settings.foundry_deployment
        or settings.llm_model
    )


def describe_routes(settings: Settings | None = None) -> list[tuple[str, str, str]]:
    """(task, tier, resolved deployment) for every route.

    Used by the registry test and worth printing when checking a deployment name
    against what somebody else configured -- which is exactly the cross-check
    Anish asked for between the two of us.
    """
    settings = settings or get_settings()
    return [
        (route.task, route.tier, model_for(route.task, settings))
        for route in sorted(ROUTES.values(), key=lambda r: r.task)
    ]
