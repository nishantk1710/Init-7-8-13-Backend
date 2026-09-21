"""What the backend hands the frontend, one step at a time.

Server-driven, and why
-----------------------
The chat mock-up drives the whole conversation in the browser. That does not
hold up for three reasons, and the third is the one that settles it:

* An abandoned session would leave **no record**, and "advice given, not acted
  on" is explicitly a number both FRSs want.
* Both FRSs require keeping **the advice as served**, because benefit
  attribution reads it months later. A browser that decides what to say is the
  only place that knows what was said.
* The script would exist twice -- once in Python, once in TypeScript -- and the
  two would drift. They would then disagree about a compliance question in front
  of a user.

So the backend owns the script and returns *the next step*; the frontend renders
whatever it is handed. The mock-up already has generic renderers for text,
choices and action buttons, so it does not need to know the script -- which is
what makes this a smaller frontend change than it sounds.

A deliberately small vocabulary
--------------------------------
Four kinds of step, no more. Every additional kind is a renderer the frontend
has to grow and a branch the state machine has to test, and the two flows have
so far needed nothing beyond these:

``MESSAGE``   something is being said; the only reply is to continue.
``CHOICE``    pick one of a fixed set.
``FORM``      fill in named fields.
``TERMINAL``  the conversation is over; here is the session ID to type into SAP.

The assessment is attached to a step as ``facts`` rather than being its own
kind. It is a card rendered above the question, not a question -- and modelling
it as a step would have meant a round trip to say something the requester cannot
answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepKind(str, Enum):
    MESSAGE = "message"
    CHOICE = "choice"
    FORM = "form"
    TERMINAL = "terminal"


class FieldType(str, Enum):
    """Just enough for the frontend to pick an input. Not a type system."""

    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"


@dataclass(frozen=True)
class Choice:
    """One option, and what picking it means."""

    value: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class Field:
    """One input on a form step."""

    name: str
    label: str
    type: FieldType
    required: bool = True
    options: tuple[Choice, ...] = ()
    help_text: str | None = None
    default: Any = None


@dataclass(frozen=True)
class Step:
    """One thing the assistant says, and what it expects back.

    Frozen, and built fresh each time rather than looked up from a table of
    steps. The prompt text depends on the assessment -- "there are 2 in stock"
    is part of the question, not decoration around it -- so a step is a value
    computed from the session, not a constant.
    """

    id: str
    """Stable across wording changes. ``i08_repair_challenge`` stays that even
    when the sentence is reworded, so "how often did anybody override this?"
    keeps answering the same question a year later."""

    kind: StepKind
    prompt: str

    choices: tuple[Choice, ...] = ()
    fields: tuple[Field, ...] = ()

    facts: dict[str, Any] | None = None
    """The assessment card rendered above the question, where there is one."""

    footnote: str | None = None
    """Caveats. Shown under the question rather than folded into it -- a
    sentence that hedges every clause is unreadable, a short list under it is
    not."""

    session_id: str | None = None
    """Set on the terminal step: the ID to type into SAP."""

    @property
    def is_terminal(self) -> bool:
        return self.kind is StepKind.TERMINAL

    @property
    def expects_answer(self) -> bool:
        return self.kind in (StepKind.CHOICE, StepKind.FORM)
