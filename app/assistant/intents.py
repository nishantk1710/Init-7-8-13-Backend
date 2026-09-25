"""The free-text box, answered from read models rather than by an agent.

The scope decision this implements
-----------------------------------
General business question-answering appears in **neither FRS**. Both specify a
*structured* flow with a defined outcome, and a general Q&A assistant is a
different product -- a tool-calling agent over the read models, or a retrieval
layer, plus its own accuracy and safety conversation.

So the structured flow was built first and completely, and this is the deliberate
minimum beside it: the free-text box stays, and it answers from a **small,
explicit set of intents over deterministic read models**. No model is involved.
Nothing is generated. Every number comes from an endpoint that already exists and
is already tested.

**General Q&A remains a separate scope item and must not be absorbed silently
into WS7.** That is what :data:`OUT_OF_SCOPE_NOTE` says out loud, on every
answer this module cannot give.

Matching the frontend's vocabulary on purpose
----------------------------------------------
``Init-7-8-13-Frontend/src/lib/chat-intents.ts`` already classifies free text
into four buckets -- ``stock``, ``repair``, ``oar`` and ``approval`` -- and
answers each from a fixture selector. The same four names are used here so the
frontend's swap is "call this instead of the local selector" rather than a
re-think.

One of the four cannot be answered, and says so
------------------------------------------------
``stock`` routes to Initiative 07 in the frontend, and **Initiative 07 is empty
stubs on this backend** (``app/initiatives/i7/``). So that intent is recognised
and then explicitly declined, naming the reason. Recognising a question and
answering it from the wrong module would be worse than not answering: the
requester cannot tell which module replied, and a reorder-point answer assembled
out of I08 register rows would be wrong in a way nobody could see.

What "no match" means
----------------------
An unmatched question gets a refusal that lists what *can* be asked. A free-text
box that silently does nothing teaches people it is broken; one that guesses
teaches them it is unreliable. Saying "I cannot answer that, here is what I can"
is the only option that leaves the requester better off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.models.i13_act_exception import ActExceptionRecord

#: Said on every refusal. The boundary is a scope decision, not a limitation to
#: apologise for, and it needs to stay visible while it is still undecided.
OUT_OF_SCOPE_NOTE = (
    "This assistant answers a fixed set of questions from the platform's own "
    "data. General question-answering over the whole dataset is a separate "
    "piece of work that has not been scoped or agreed."
)


class Intent(str, Enum):
    """The questions this box can answer. Names match the frontend's buckets."""

    REPAIR = "repair"
    OAR = "oar"
    APPROVAL = "approval"
    STOCK = "stock"
    """Recognised, and then declined -- Initiative 07 is not built here."""

    NONE = "none"


#: Keyword buckets, checked in this order. Copied in spirit from the frontend's
#: own list so the two agree about what a question means; kept as data rather
#: than as a chain of ifs so adding a phrase is not a code change in shape.
#:
#: Order matters: "approval" is checked first because "waiting for approval on
#: an overdue repair" is an approvals question, not a repairs question.
KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.APPROVAL: (
        "my approval",
        "needs approval",
        "waiting for my decision",
        "pending approval",
        "approvals",
        "waiting on me",
        "confirm",
    ),
    Intent.REPAIR: (
        "under repair",
        "repair chain",
        "overdue repair",
        "repairs are overdue",
        "duplicate procurement",
        "coming back",
        "at the vendor",
        "being repaired",
    ),
    Intent.OAR: (
        "oar material",
        "oar materials",
        "planned use",
        "planned consumption",
        "redeploy",
        "unused stock",
        "another plant",
        "other plant",
        "no plan",
    ),
    Intent.STOCK: (
        "critical spares",
        "at risk",
        "stock-out",
        "stockout",
        "excess stock",
        "excess inventory",
        "reorder point",
        " rop ",
        "safety stock",
    ),
}


@dataclass(frozen=True)
class IntentAnswer:
    """A deterministic answer, and where every number in it came from."""

    intent: Intent
    answered: bool
    text: str

    sources: tuple[str, ...] = ()
    """The endpoints or read models behind the numbers, named so a reader can
    go and check them rather than trust the sentence."""

    data: dict[str, Any] = field(default_factory=dict)
    suggestions: tuple[str, ...] = ()
    note: str = OUT_OF_SCOPE_NOTE


def classify(text: str) -> Intent:
    """Which bucket a question falls into, or :attr:`Intent.NONE`.

    Substring matching, padded with spaces so " rop " cannot match inside
    "europe". Deliberately dumb: a smarter classifier would be a model, and a
    model here would be the general-Q&A product this is explicitly not.
    """
    haystack = f" {(text or '').lower().strip()} "
    for intent, needles in KEYWORDS.items():
        if any(needle in haystack for needle in needles):
            return intent
    return Intent.NONE


#: What the box can be asked, in the requester's own words. Served with every
#: refusal, and available to the frontend as the suggestion chips above the
#: input.
ANSWERABLE: tuple[str, ...] = (
    "Which repairs are overdue?",
    "Which OAR materials have no consumption plan?",
    "What is waiting on my decision?",
)


def _repair(db: DbSession) -> IntentAnswer:
    """Open and overdue repairs, from the I08 register.

    Deliberately reads the same cached snapshot the register screen serves, so
    the chat and the screen beside it can never quote different numbers for the
    same question.
    """
    from app.initiatives.i8.service import get_snapshot

    snapshot = get_snapshot(db)
    open_lines = [line for line in snapshot.lines if line.is_open]
    overdue = [line for line in open_lines if line.is_overdue]

    if not open_lines:
        return IntentAnswer(
            intent=Intent.REPAIR,
            answered=True,
            text="No repairs are open in this extract.",
            sources=("/api/i8/register",),
            data={"openRepairs": 0, "overdueRepairs": 0},
        )

    worst = sorted(
        (line for line in overdue if line.due_date is not None),
        key=lambda line: line.due_date,
    )[:3]
    listed = "\n".join(
        f"- {line.material_id} at plant {line.plant or 'unknown'}, "
        f"PO {line.purchasing_document}/{line.item}, due {line.due_date.isoformat()}"
        for line in worst
    )

    text = (
        f"{len(open_lines)} repairs are open and {len(overdue)} of them are past "
        f"their promised return date."
    )
    if listed:
        text += f"\n\nThe longest outstanding:\n{listed}"
    text += (
        "\n\nNote that a repair being open means the purchase order exists -- no "
        "dispatch movement is recorded against any open line in this extract, so "
        "none of these can be confirmed as physically with the vendor."
    )

    return IntentAnswer(
        intent=Intent.REPAIR,
        answered=True,
        text=text,
        sources=("/api/i8/register",),
        data={"openRepairs": len(open_lines), "overdueRepairs": len(overdue)},
    )


def _oar(db: DbSession) -> IntentAnswer:
    """OAR materials with an open ACT exception.

    Counts by type from the persisted exception store rather than re-running
    detection: a free-text question must not trigger a detection run.
    """
    rows = db.execute(
        select(
            ActExceptionRecord.exception_type,
            func.count(ActExceptionRecord.exception_id),
        )
        .where(ActExceptionRecord.status.notin_(("RESOLVED",)))
        .group_by(ActExceptionRecord.exception_type)
    ).all()

    counts = {exception_type: count for exception_type, count in rows}
    total = sum(counts.values())

    if not total:
        return IntentAnswer(
            intent=Intent.OAR,
            answered=True,
            text=(
                "No OAR exceptions are open. Note that detection has to have "
                "been run for that to mean anything -- see POST /api/i13/act/run/detect."
            ),
            sources=("/api/i13/act/exceptions",),
            data={"openExceptions": 0},
        )

    breakdown = "\n".join(
        f"- {name.replace('_', ' ').title()}: {count}"
        for name, count in sorted(counts.items(), key=lambda pair: -pair[1])
    )
    return IntentAnswer(
        intent=Intent.OAR,
        answered=True,
        text=f"{total} OAR exceptions are open:\n\n{breakdown}",
        sources=("/api/i13/act/exceptions",),
        data={"openExceptions": total, "byType": counts},
    )


def _approval(db: DbSession) -> IntentAnswer:
    """Exceptions waiting on a person.

    The nearest real thing to "approvals" that this backend holds. There is no
    approvals engine here, and saying so is better than answering a different
    question that happens to have a number.
    """
    waiting = db.execute(
        select(func.count(ActExceptionRecord.exception_id)).where(
            ActExceptionRecord.status == "AWAITING_REQUESTER"
        )
    ).scalar_one()

    escalated = db.execute(
        select(func.count(ActExceptionRecord.exception_id)).where(
            ActExceptionRecord.status == "ESCALATED"
        )
    ).scalar_one()

    text = (
        f"{waiting} exceptions are waiting on their requester to confirm, and "
        f"{escalated} have escalated to a head of department."
    )
    if escalated:
        text += (
            "\n\nEscalations currently route to nobody: no HOD list is configured "
            "(i13_hod_recipients is empty), so their routing is PENDING rather "
            "than assigned to an invented recipient."
        )
    text += (
        "\n\nThis is the exception queue, not an approvals engine -- there is no "
        "approvals workflow on this backend."
    )

    return IntentAnswer(
        intent=Intent.APPROVAL,
        answered=True,
        text=text,
        sources=("/api/i13/act/exceptions",),
        data={"awaitingRequester": waiting, "escalated": escalated},
    )


def _stock_declined() -> IntentAnswer:
    """Recognised, then declined. Initiative 07 is not built on this backend."""
    return IntentAnswer(
        intent=Intent.STOCK,
        answered=False,
        text=(
            "That is an inventory-planning question -- reorder points, safety "
            "stock and excess. Initiative 07 is not built on this backend yet, "
            "so there is no source to answer it from.\n\n"
            "Answering it from the repair register or the utilisation mart "
            "instead would produce a number that looks right and is not, so "
            "nothing is offered."
        ),
        suggestions=ANSWERABLE,
    )


def _unmatched() -> IntentAnswer:
    listed = "\n".join(f"- {question}" for question in ANSWERABLE)
    return IntentAnswer(
        intent=Intent.NONE,
        answered=False,
        text=f"That is not something this assistant can answer. It can answer:\n\n{listed}",
        suggestions=ANSWERABLE,
    )


def answer(
    db: DbSession, text: str, settings: Settings | None = None
) -> IntentAnswer:
    """Answer a free-text question, or say plainly that it cannot.

    Never raises for an unrecognised question -- "I do not know" is a valid
    answer and the commonest one.
    """
    settings = settings or get_settings()

    if not settings.assistant_free_text_intents_enabled:
        return IntentAnswer(
            intent=Intent.NONE,
            answered=False,
            text=(
                "Free-text questions are switched off "
                "(assistant_free_text_intents_enabled)."
            ),
        )

    intent = classify(text)

    if intent is Intent.REPAIR:
        return _repair(db)
    if intent is Intent.OAR:
        return _oar(db)
    if intent is Intent.APPROVAL:
        return _approval(db)
    if intent is Intent.STOCK:
        return _stock_declined()
    return _unmatched()
