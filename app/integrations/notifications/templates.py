"""Wording for every notification: one subject, body and button label per event.

Everything a person reads comes from here, so it is also the one place to check
the wording rule: no internal module names (I07, I08, I13) in anything a user
sees. ``tests/test_notification_templates.py`` enforces it.

Each template is a plain ``str.format`` string. A placeholder the caller did not
supply renders as a dash rather than raising, so a missing detail never stops a
notification.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Event types (stored on the notification row) ----------------------------

APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
APPROVAL_SENT_BACK = "APPROVAL_SENT_BACK"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_ON_HOLD = "APPROVAL_ON_HOLD"
APPROVAL_HOLD_RELEASED = "APPROVAL_HOLD_RELEASED"
APPROVAL_ADJUSTED = "APPROVAL_ADJUSTED"
APPROVAL_ADJUSTED_FYI = "APPROVAL_ADJUSTED_FYI"
APPROVAL_COMPLETED = "APPROVAL_COMPLETED"
APPROVAL_REMINDER = "APPROVAL_REMINDER"
APPROVAL_ESCALATED = "APPROVAL_ESCALATED"
# After the final approval: tell the Inventory Controller to change the approved values in SAP, remind them while the
# monitoring window is open (the last reminder is the one-day-left warning), and say how it ended.
APPROVAL_SAP_UPDATE_REQUIRED = "APPROVAL_SAP_UPDATE_REQUIRED"
APPROVAL_SAP_UPDATE_REMINDER = "APPROVAL_SAP_UPDATE_REMINDER"
APPROVAL_SAP_UPDATE_FINAL_REMINDER = "APPROVAL_SAP_UPDATE_FINAL_REMINDER"
APPROVAL_SAP_UPDATE_RESULT = "APPROVAL_SAP_UPDATE_RESULT"
EXCEPTION_ACTION_REQUIRED = "EXCEPTION_ACTION_REQUIRED"
EXCEPTION_RESPONSE_REQUIRED = "EXCEPTION_RESPONSE_REQUIRED"
EXCEPTION_ESCALATED = "EXCEPTION_ESCALATED"
# The requester has not answered yet: sent every few days until they do, or it goes to the HOD.
EXCEPTION_REMINDER = "EXCEPTION_REMINDER"
# A purchase requisition SAP posted to /api/events/pr. The plain notice goes to every
# signed-in user and names nothing about the requisition; the details are for administrators.
PR_RECEIVED = "PR_RECEIVED"
PR_RECEIVED_DETAILS = "PR_RECEIVED_DETAILS"
# A request's reservation, from the two people it concerns. The person who raised the request is the one who creates
# the reservation in SAP and links it: told when the request is raised (LINK_REQUIRED) and reminded a day later if
# a line is still unlinked (LINK_REMINDER). The person named in its Requester box is told who raised it and that it is
# waiting for linking (REQUESTED), and reminded when it still is (LINK_PENDING). Both are told when it is linked.
RESERVATION_LINK_REQUIRED = "RESERVATION_LINK_REQUIRED"
RESERVATION_REQUESTED = "RESERVATION_REQUESTED"
RESERVATION_LINKED = "RESERVATION_LINKED"
RESERVATION_LINK_REMINDER = "RESERVATION_LINK_REMINDER"
RESERVATION_LINK_PENDING = "RESERVATION_LINK_PENDING"
GENERAL = "GENERAL"


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    button_label: str


@dataclass(frozen=True)
class _Template:
    title: str
    body: str
    button_label: str


_REVIEW = "Open Review Workspace"
_EXCEPTION = "Open Exception"
_REQUEST = "Open Request"

_TEMPLATES: dict[str, _Template] = {
    APPROVAL_REQUIRED: _Template(
        "Approval Required – Material Recommendation",
        "A new inventory recommendation for Plant {plant} is waiting for your approval.\n"
        "Material: {material}\n"
        "Action required: Review and approve the recommendation.",
        _REVIEW,
    ),
    APPROVAL_SENT_BACK: _Template(
        "Recommendation Sent Back – Material {material}",
        "A recommendation for Plant {plant} was sent back for correction.\n"
        "Material: {material}\n"
        "Comment: {comment}",
        _REVIEW,
    ),
    APPROVAL_REJECTED: _Template(
        "Recommendation Rejected – Material {material}",
        "A recommendation for Plant {plant} was rejected.\n"
        "Material: {material}\n"
        "Comment: {comment}",
        _REVIEW,
    ),
    APPROVAL_ON_HOLD: _Template(
        "Recommendation On Hold – Material {material}",
        "A recommendation for Plant {plant} was put on hold by the approver.\n"
        "Material: {material}\n"
        "Comment: {comment}",
        _REVIEW,
    ),
    APPROVAL_HOLD_RELEASED: _Template(
        "Hold Released – Material {material}",
        "The hold on a recommendation for Plant {plant} was released and it is moving again.\n"
        "Material: {material}",
        _REVIEW,
    ),
    APPROVAL_ADJUSTED: _Template(
        "Recommendation Adjusted – Approval Required",
        "A recommendation for Plant {plant} was adjusted by the {adjusted_by} and is waiting for your approval.\n"
        "Material: {material}\n"
        "Change and comment: {comment}\n"
        "Action required: Review the adjusted values.",
        _REVIEW,
    ),
    APPROVAL_ADJUSTED_FYI: _Template(
        "Recommendation Adjusted – Material {material}",
        "A recommendation for Plant {plant} was adjusted by the {adjusted_by}. It has moved on to the {pending_role}.\n"
        "Material: {material}\n"
        "Change and comment: {comment}\n"
        "For your information: nothing is needed from you.",
        _REVIEW,
    ),
    APPROVAL_COMPLETED: _Template(
        "Recommendation Approved – Material {material}",
        "A recommendation for Plant {plant} has been fully approved.\n"
        "Material: {material}\n"
        "Next step: the approved values are updated in SAP by the team.",
        _REVIEW,
    ),
    APPROVAL_REMINDER: _Template(
        "Reminder: Approval Required – Material {material}",
        "A recommendation for Plant {plant} is still waiting for your approval.\n"
        "Material: {material}\n"
        "Action required: Review and approve the recommendation.",
        _REVIEW,
    ),
    APPROVAL_ESCALATED: _Template(
        "Escalation Required – Approval Overdue for Material {material}",
        "A recommendation for Plant {plant} has waited too long for approval from {pending_role}.\n"
        "Material: {material}\n"
        "Action required: Follow up with the approver.",
        _REVIEW,
    ),
    APPROVAL_SAP_UPDATE_REQUIRED: _Template(
        "Approved – Update SAP for Material {material}",
        "A recommendation for Plant {plant} was fully approved. The approved values now need to be changed in SAP.\n"
        "Material: {material}\n"
        "Final approver: {approver_role}\n"
        "{values}\n"
        "Deadline: {deadline}\n"
        "Action required: update these values in SAP by the deadline. If SAP is not confirmed to match them by then, "
        "the recommendation is marked Not adopted.",
        _REVIEW,
    ),
    APPROVAL_SAP_UPDATE_REMINDER: _Template(
        "Reminder: SAP Update Pending – Material {material}",
        "The approved recommendation for Plant {plant} is not yet confirmed in SAP.\n"
        "Material: {material}\n"
        "Final approver: {approver_role}\n"
        "{values}\n"
        "Deadline: {deadline} ({days_left} day(s) left)\n"
        "Action required: update these values in SAP. If SAP is not confirmed to match them by the deadline, "
        "the recommendation is marked Not adopted.",
        _REVIEW,
    ),
    APPROVAL_SAP_UPDATE_FINAL_REMINDER: _Template(
        "Final Reminder: SAP Update Due {deadline} – Material {material}",
        "The monitoring window for the approved recommendation at Plant {plant} closes on {deadline}; "
        "the SAP update is not yet confirmed.\n"
        "Material: {material}\n"
        "Final approver: {approver_role}\n"
        "{values}\n"
        "Deadline: {deadline} ({days_left} day(s) left)\n"
        "Action required: update these values in SAP now. If SAP is not confirmed to match them by the deadline, "
        "the recommendation is marked Not adopted.",
        _REVIEW,
    ),
    APPROVAL_SAP_UPDATE_RESULT: _Template(
        "SAP Update {outcome} – Material {material}",
        "The SAP check for the approved recommendation at Plant {plant} is complete: {outcome}.\n"
        "Material: {material}\n"
        "Final approver: {approver_role}\n"
        "{values}\n"
        "Deadline was: {deadline}\n"
        "{detail}",
        _REVIEW,
    ),
    EXCEPTION_ACTION_REQUIRED: _Template(
        "Action Required – Inventory Exception",
        "{summary}\n"
        "Material: {material}\n"
        "Plant: {plant}",
        _EXCEPTION,
    ),
    EXCEPTION_RESPONSE_REQUIRED: _Template(
        "Action Required – Inventory Exception",
        "{summary}\n"
        "Action required: Please confirm or explain.",
        _EXCEPTION,
    ),
    EXCEPTION_REMINDER: _Template(
        "Reminder: Action Required – Inventory Exception",
        "{summary}\n"
        "Action required: Please confirm or explain before it goes to your HOD.",
        _EXCEPTION,
    ),
    EXCEPTION_ESCALATED: _Template(
        "Escalation Required – Inventory Exception",
        "{summary}\n"
        "This exception was not answered in time and needs your review.",
        _EXCEPTION,
    ),
    # No placeholders on purpose: this one reaches everyone, so it carries nothing about the requisition.
    PR_RECEIVED: _Template(
        "New Purchase Requisition Received",
        "A new purchase requisition was received from SAP.",
        "Open",
    ),
    PR_RECEIVED_DETAILS: _Template(
        "Purchase Requisition {banfn} Received",
        "Purchase requisition {banfn} was received from SAP.\n"
        "Created on: {created_on} at {created_at}\n"
        "Created by (SAP user): {created_by}\n"
        "SAP message: {message}",
        "Open",
    ),
    RESERVATION_LINK_REQUIRED: _Template(
        "Action Required: Link the Reservation – Request {request}",
        "You raised request {request} at Plant {plant} for {requested_for}. No SAP reservation is linked to these "
        "lines yet:\n"
        "{lines}\n"
        "Action required: create the reservation in SAP with {request} in its header text, "
        "then press Link reservation on the request.",
        _REQUEST,
    ),
    RESERVATION_REQUESTED: _Template(
        "Reservation Requested for You – Request {request}",
        "{raised_by} raised request {request} at Plant {plant} for you. It is waiting for its SAP reservation to be "
        "linked:\n"
        "{lines}\n"
        "{raised_by} creates the reservation in SAP and links it. You will be told when it is linked.",
        _REQUEST,
    ),
    RESERVATION_LINKED: _Template(
        "Reservation Linked – Request {request}",
        "Your SAP reservation is now linked to request {request} at Plant {plant}.\n"
        "{lines}\n"
        "Lines with a reservation: {linked} of {total}.",
        _REQUEST,
    ),
    RESERVATION_LINK_REMINDER: _Template(
        "Reminder: Reservation Not Linked – Request {request}",
        "No SAP reservation is linked yet to these lines of request {request} at Plant {plant}:\n"
        "{lines}\n"
        "Action required: create the reservation in SAP with {request} in its header text, "
        "then press Link reservation on the request.",
        _REQUEST,
    ),
    RESERVATION_LINK_PENDING: _Template(
        "Reminder: Reservation Not Linked Yet – Request {request}",
        "Request {request}, raised for you by {raised_by} at Plant {plant}, still has no SAP reservation linked to "
        "these lines:\n"
        "{lines}\n"
        "{raised_by} needs to create the reservation in SAP and link it. You will be told when it is linked.",
        _REQUEST,
    ),
    GENERAL: _Template("Notification", "{summary}", "Open"),
}


class _Dash(dict):
    """``format_map`` source that renders a missing placeholder as a dash."""

    def __missing__(self, key: str) -> str:
        return "-"


def render(event_type: str, context: dict[str, str] | None = None) -> Rendered:
    """Fill the template for ``event_type``. An unknown type uses the general one."""
    template = _TEMPLATES.get(event_type, _TEMPLATES[GENERAL])
    values = _Dash({k: ("-" if v in (None, "") else str(v)) for k, v in (context or {}).items()})
    return Rendered(
        title=template.title.format_map(values),
        body=template.body.format_map(values),
        button_label=template.button_label,
    )


def known_event_types() -> list[str]:
    return sorted(_TEMPLATES)
