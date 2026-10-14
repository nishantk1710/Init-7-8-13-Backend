"""The summary report's words: a model's when it can, a template's otherwise.

The figures are already fixed (``facts.py``). The model is handed them as JSON
and asked for a headline, findings, risks and actions. Two rules make that
safe to put in front of plant management:

* **No new numbers.** Every number in the model's text must already appear in
  the facts (a percentage the facts carry counts; one the model worked out
  itself does not). A reply that breaks this is asked for once more, then
  dropped for the template. A report that is plain but right beats one that is
  fluent and wrong.
* **The screens' words.** The facts carry internal codes (``NO_PLAN``); the
  model is given the screens' word for each (``labels.py``) and a reply that
  still shows a code is treated like one with an invented number.
* **Never a failure.** No provider, the stub, a timeout, malformed JSON -- each
  falls back to the template, exactly as ``quantity_suggestion_reason`` does.
  The report always generates.

``source`` records which one wrote the words (``MODEL`` or ``DETERMINISTIC``),
with the prompt id, version and model when it was a model, so a reader -- and
an auditor later -- can tell at a glance.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.ai import AIError
from app.core.config import Settings, get_settings
from app.core.prompts import PromptError, complete_with_prompt
from app.initiatives.i13.report.labels import CODES, EXCEPTION_STATUS, glossary

logger = logging.getLogger(__name__)

PROMPT_ID = "i13_dashboard_report"

DETERMINISTIC = "DETERMINISTIC"
MODEL = "MODEL"

#: Small counting words a model writes as digits ("the 3 actions below") --
#: harmless, and refusing them would throw away good reports for nothing.
_FREE_NUMBERS = frozenset(str(n) for n in range(0, 11))

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class Action:
    action: str
    owner: str


@dataclass(frozen=True)
class Narrative:
    headline: str
    findings: list[str]
    risks: list[str]
    actions: list[Action]
    source: str
    prompt_id: str | None = None
    prompt_version: int | None = None
    model: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- the number rule ---------------------------------------------------------


def _canonical(token: str) -> str | None:
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def numbers_in(text: str) -> set[str]:
    found = set()
    for token in _NUMBER.findall(text):
        canonical = _canonical(token.rstrip(","))
        if canonical is not None:
            found.add(canonical)
    return found


def allowed_numbers(facts: dict[str, Any]) -> set[str]:
    """Every number the facts hold, in any field, including the digits inside
    identifiers and dates -- a material number or a reservation quoted back is
    not an invented figure."""
    allowed = set(_FREE_NUMBERS)
    for token in re.findall(r"\d+(?:\.\d+)?", json.dumps(facts, default=str)):
        canonical = _canonical(token)
        if canonical is not None:
            allowed.add(canonical)
    return allowed


def invented_numbers(narrative: Narrative, facts: dict[str, Any]) -> set[str]:
    text = " ".join(
        [narrative.headline, *narrative.findings, *narrative.risks]
        + [f"{a.action} {a.owner}" for a in narrative.actions]
    )
    return numbers_in(text) - allowed_numbers(facts)


def leaked_codes(narrative: Narrative) -> set[str]:
    """Internal codes (``AWAITING_REQUESTER``) that reached the prose."""
    text = " ".join(
        [narrative.headline, *narrative.findings, *narrative.risks]
        + [f"{a.action} {a.owner}" for a in narrative.actions]
    )
    return {code for code in CODES if "_" in code and code in text}


# --- the model's reply ----------------------------------------------------------


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def parse_reply(text: str) -> Narrative | None:
    """The model's JSON, or ``None`` when it is not the shape asked for."""
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    headline = str(data.get("headline") or "").strip()
    findings = _strings(data.get("findings"), 6)
    actions = []
    for item in data.get("actions") or []:
        if isinstance(item, dict) and str(item.get("action") or "").strip():
            actions.append(
                Action(action=str(item["action"]).strip(), owner=str(item.get("owner") or "").strip() or "Unassigned")
            )
    if not headline or not findings:
        return None
    return Narrative(
        headline=headline,
        findings=findings,
        risks=_strings(data.get("risks"), 5),
        actions=actions[:5],
        source=MODEL,
    )


# --- the template ------------------------------------------------------------------


def _n(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def template_narrative(facts: dict[str, Any], *, note: str | None = None) -> Narrative:
    """The same report in fixed sentences. Every figure comes straight from
    ``facts``, so this can never fail the number rule."""
    pos = facts["positions"]
    plans = facts["plans"]
    grni = facts["gr_not_issued"]
    exc = facts["exceptions"]
    status = exc["by_status"]
    scope = facts["scope"]["label"]

    headline = (
        f"{scope}: {_n(pos['NON_MOVING'])} of {_n(pos['total'])} OAR materials are non-moving"
        + (f" ({pos['non_moving_pct']}%)" if pos["non_moving_pct"] is not None else "")
        + f", and {_n(exc['unresolved'])} procurement exceptions are still open."
    )
    findings = [
        f"Stock type: {_n(pos['FAST'])} fast-moving, {_n(pos['SLOW'])} slow-moving and "
        f"{_n(pos['NON_MOVING'])} non-moving OAR materials.",
        f"Plans: {_n(plans['plan_breach_count'])} Planned Overdue and {_n(plans['no_plan_count'])} "
        "Non-AI (reserved without a consumption plan).",
        f"Received not issued: {_n(grni['open_line_count'])} reservation lines were received at least "
        f"{grni['threshold_days']} days ago and have not been issued.",
        f"Procurement exceptions: {_n(status.get('ESCALATED', 0))} {EXCEPTION_STATUS['ESCALATED'].lower()}, "
        f"{_n(status.get('AWAITING_REQUESTER', 0))} {EXCEPTION_STATUS['AWAITING_REQUESTER'].lower()} and "
        f"{_n(status.get('OPEN', 0))} where the requester is not identified.",
    ]
    risks = []
    if grni["longest_waiting"]:
        oldest = grni["longest_waiting"][0]
        name = oldest["description"] or oldest["material"]
        risks.append(
            f"The longest-waiting received line ({name}, reservation {oldest['reservation']}) "
            f"has not been issued for {oldest['days_since_gr']} days."
        )
    if pos["NON_MOVING"]:
        risks.append("Non-moving OAR stock ties up working capital and may become obsolete.")
    if status.get("OPEN", 0):
        risks.append("Exceptions with no identified requester cannot be followed up until an owner is found.")
    actions = [
        Action("Follow up the longest-waiting received-not-issued lines with the requesters.", "Plant stores"),
        Action("Give a reason for or close the exceptions assigned to requesters.", "Requester"),
        Action("Review the exceptions escalated to HOD.", "HOD"),
    ]
    notes = [note] if note else []
    return Narrative(headline, findings, risks, actions, source=DETERMINISTIC, notes=notes)


# --- the entry point ------------------------------------------------------------------


def _model_available(settings: Settings) -> bool:
    """A *real* provider, not the stub -- the stub's text is openly synthetic."""
    return (settings.llm_provider or "stub").strip().lower() != "stub" and settings.llm_configured


def _ask(facts: dict[str, Any], correction: str) -> tuple[Narrative | None, Any]:
    completion = complete_with_prompt(
        PROMPT_ID,
        facts_json=json.dumps(facts, indent=1, default=str),
        glossary=glossary(),
        correction=correction,
        max_tokens=1500,
        temperature=0.2,
    )
    return parse_reply(completion.text or ""), completion


def write_narrative(facts: dict[str, Any], *, settings: Settings | None = None) -> Narrative:
    """The narrative for one report. There is no failure path out of here."""
    settings = settings or get_settings()
    if not _model_available(settings):
        return template_narrative(facts, note="No AI provider is configured; standard wording used.")

    correction = ""
    for attempt in (1, 2):
        try:
            narrative, completion = _ask(facts, correction)
        except (AIError, PromptError) as exc:
            logger.warning("i13 summary report fell back to the template: %s", exc)
            return template_narrative(facts, note="The AI service did not answer; standard wording used.")

        if narrative is None:
            logger.warning("i13 summary report: reply %d was not the JSON asked for", attempt)
            correction = "Your previous reply was not valid JSON in the required shape. Reply with the JSON object only."
            continue

        leaked = leaked_codes(narrative)
        if leaked:
            logger.warning("i13 summary report: reply %d used internal codes: %s", attempt, sorted(leaked))
            correction = (
                "Your previous reply used these internal codes: "
                f"{', '.join(sorted(leaked))}. Use the words from the glossary instead."
            )
            continue

        invented = invented_numbers(narrative, facts)
        if invented:
            logger.warning("i13 summary report: reply %d used numbers not in the facts: %s", attempt, sorted(invented))
            correction = (
                "Your previous reply used these numbers, which are not in the facts: "
                f"{', '.join(sorted(invented))}. Use only numbers that appear in the facts."
            )
            continue

        return Narrative(
            headline=narrative.headline,
            findings=narrative.findings,
            risks=narrative.risks,
            actions=narrative.actions,
            source=MODEL,
            prompt_id=completion.prompt_id,
            prompt_version=completion.prompt_version,
            model=completion.model,
        )

    return template_narrative(facts, note="The AI text did not pass the figure check; standard wording used.")
