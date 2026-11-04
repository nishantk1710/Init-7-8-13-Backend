# Shared notifications

One notification service for approvals and OAR exceptions. It lives in
`app/integrations/notifications/`; nothing else builds its own client.

## How it works

1. **Something happens** -- an approval moves (`app/initiatives/i7/notifications.py`) or an exception goes
   unanswered (I13, `app/initiatives/i13/act/service.py`). Repairable-spares (I08) exceptions were notified here too
   until 3-Nov-2026, when their exception queue was removed.
2. **`enqueue`** writes `notification` rows in the caller's own transaction -- an **in-app** row (the inbox) and an
   **email** row for each recipient. A step that fails (HTTP 409) never gets this far, and a step that is saved
   always has its notifications saved with it.
3. **The scheduler** (`app/integrations/notifications/scheduler.py`, started from `app/main.py`) sends the pending
   emails through the configured adapter and records what happened.

### Honest statuses

| Status | Meaning |
| --- | --- |
| `SENT` | An in-app row (it is in the inbox), or an email the provider accepted (Graph `202`). |
| `PENDING` / `PROCESSING` | An email waiting to be sent / being sent. Nothing delivered yet. |
| `LOGGED_ONLY` | The logging adapter ran. **Nothing was sent.** Final. |
| `FAILED` | Not delivered, with the reason in `error`. Retried up to `NOTIFICATION_RETRY_MAX` times. A recipient nobody could resolve is also `FAILED`, with the reason. |

Until Microsoft Graph is configured every email ends as `LOGGED_ONLY`. Everything else -- notifications, the inbox,
recipient resolution, reminders, escalation, the audit trail -- works without it.

### Who is acting, and who can be a recipient

- **The actor is the authenticated caller.** The I07 approval routes record -- and notify -- the Entra object id
  from the token (`get_i7_actor`; the `X-Actor-Id` header only while sign-in is off). The `actor_id` field in the body
  is **ignored**, and the frontend no longer sends one. A body can no longer name the submitter or an earlier approver.
- **A person is an Entra object id (a GUID), never an email address.** An identity that is not a GUID -- an email,
  a name, a placeholder such as `end-user` -- is refused as an *invalid identity* and recorded as a failed
  notification; it is never turned into an address to send to. Emails come only from `app_user`, the
  administrator-maintained `notification_recipients` row, or `NOTIFICATION_FALLBACK_EMAIL`.
- Consequence for **I13**: the requester and the HOD (`I13_HOD_RECIPIENTS`, `PLANT:identity`) must be Entra object ids
  to be notified. A SAP user name (e.g. from RESB `Wempf`) or an email address is recorded as a failed notification
  with the reason. Moving the HOD into `notification_recipients` is still to do.
- **Still open (not changed here): approval authorisation.** `actor_role` in the body is still the caller's own claim;
  the approval routes have no `require_roles` and do not compare it with the roles in the token (see
  `tests/test_write_roles.py`, PENDING). Any signed-in user can act as any approval role. That is a change to the
  approval workflow and needs its own decision.

### A missing email is never silent

When a person is known but no email can be found, their in-app notification is still created and a **failed email row**
records why:

| Reason | Meaning |
| --- | --- |
| `missing identity` | No recipient was given. |
| `invalid identity` | Not an Entra object id. |
| `unknown user` | A valid id, but the person has never signed in, so `app_user` has no email. |
| `no email address on record` | Signed in, but with no email address. |

`NOTIFICATION_FALLBACK_EMAIL`, if set, is used instead of failing for the last two. It is never used for an invalid
identity.

### An approval never fails because of a notification

Notifications are written inside a database **savepoint**. If the database refuses them, only the notification is
rolled back: the approval's own session stays usable and the approval commits. The reverse also holds -- a failed
approval leaves no notification, because they share one transaction. Values are checked against the column widths
before writing and refused, never truncated: the idempotency key is replaced by its SHA-256 when it would exceed 255
characters, and an over-long address or an identity that is not a GUID becomes a failed row without being stored.
Failures are logged with the module, event, entity and error type, but not the values.

### In-app only (email switched off)

`NOTIFICATION_EMAIL_ENABLED=false` makes the in-app inbox the only channel. No email row is created, nothing is sent
(waiting email rows are left untouched), and I13 email intents do nothing. Everything else is unchanged: approvals,
reminders, escalation and the inbox work as before, and a notification that reaches nobody is still recorded as a
failed row (as an in-app row with no user, so no inbox shows it). The Microsoft Graph setup is not needed in this mode.
The default is `true`. Switching it back on later needs only the setting (and Graph, if real email is wanted).

### Wording

Everything a person reads comes from `templates.py`. No internal module name (I07, I08, I13) appears in a subject,
body, title or description; `tests/notifications/test_basics.py` enforces that. The module is kept in the `module`
column for debugging and audit only, and the inbox API does not return it.

## What triggers what (approvals)

Read from the recommendation's own route and position, never hard-coded.

| Workflow step | Told |
| --- | --- |
| Submit | The first approver |
| Approve | The next approver (or, after the last one, the submitter and the SAP execution role) |
| Send back | The previous-step approver and the submitter |
| Reject | The submitter and every earlier approver |
| Hold | The submitter |
| Release hold | The submitter and the pending approver |
| Adjust | The pending approver's role holders |

The person who just acted is not told about their own action (except that an approver asked to act always is).

## A purchase requisition from SAP (the PR event)

When SAP posts a requisition to `/api/events/pr`, the inbox is told once SAP has its `202`
(`app/services/pr_event_notifications.py`, a background task -- it never delays or fails the event):

| Who | What they see |
| --- | --- |
| Everyone who has signed in (`app_user`) | **New Purchase Requisition Received** -- "A new purchase requisition was received from SAP." Nothing else: no PR number, creator or message, and the row's `entityId` is empty, so the inbox shows no "Ref". |
| Active `Spares.Admin` rows in `notification_recipients` (any plant) | **Purchase Requisition {BANFN} Received** -- the PR number, created on/at, the SAP user who created it and SAP's message. Instead of the plain notice, not as well. |

- **In-app only**, whatever `NOTIFICATION_EMAIL_ENABLED` says: an email to every user for every requisition would be noise.
- The event has **no plant** and its `CREATED_BY` is a SAP user name, not an Entra identity, so the creator is shown
  to administrators but never notified.
- **No administrator configured** is recorded as a failed in-app row with the reason, like any unresolved recipient.
- **The same event twice** (same BANFN, date and time) adds nothing. An event without a BANFN cannot be matched to a
  repeat, so each one is its own notice.
- `PR_EVENT_NOTIFICATIONS_ENABLED=false` switches it off.
- Where people see it: **More → Notifications** (`/notifications`), with the unread badge on **More**. There is no
  page to open, so clicking one only marks it read.

## Configuration checklist

### Before anything is delivered to a person's inbox

- [ ] Apply the migration `b7e3a91d5c28` (`alembic upgrade head`, or `RUN_MIGRATIONS_ON_STARTUP=true`).
- [ ] **Recipients** -- add one `notification_recipients` row per approval role per plant (1300 Black Mountain and
  1500 Gamsberg). Use the Entra **app role value** in `role` and the person's Entra object id in `user_oid`.
  Email comes from `app_user` once they have signed in; the row's `email` is only a fallback for someone who has not.
  ```
  POST /api/notifications/admin/recipients          (Spares.Admin only)
  { "role": "Spares.EngineeringHead", "plant": "1300", "user_oid": "<object id>" }
  POST /api/notifications/admin/recipients/{id}/update   -- change, or retire with "active": false
  ```
- [ ] **Confirm the Inventory Controller step.** The approval step "Inventory Controller" is mapped to
  `Spares.InventoryControllerApprover` (the stage role in `security.APPROVAL_STAGE_ROLES`), **not**
  `Spares.InventoryController`. See `APPROVAL_ROLE_TO_ENTRA` in `app/initiatives/i7/notifications.py`.
- [ ] Optional: `NOTIFICATION_FALLBACK_EMAIL` -- used when no approver is configured for a role and plant. Blank means
  the gap is recorded as a failed notification instead.
- [ ] Optional: `NOTIFICATION_SAP_EXECUTION_ROLE` -- the Entra role of whoever applies an approved change in SAP.
  Blank means the final-approval notice goes to the submitter only.
- Repairable Spares (I08) sends **no** in-app or email notification, so there is nothing to configure for it
  (`I8_NOTIFICATION_RECIPIENT_ROLES` no longer exists; remove it from any environment that still sets it). It has no
  exception queue or log either (removed 3-Nov-2026). Notifications it sent earlier stay in the table but are kept out of every inbox, unread count and
  mark-all-read. Emails it had already queued as `PENDING` are still sent unless cancelled: check for
  `module = 'I08'` rows in `notification` with `status = 'PENDING'` and cancel them if they should not go out.
- [ ] **I13 requester and HOD** -- see "I13 requester and HOD" below. Both need an Entra object id, and the requester
  needs a mapping from their SAP name.

### Reminders and escalation (off until you set them)

With no active row in `notification_sla` **and no `I7_*` settings below, nothing is reminded or escalated**. The notices
sent when an approval moves are not affected.

**Option A -- production settings, in days (simplest).** They apply to every approval step that has no `notification_sla`
row, and everything is off until you set them:

| Setting | Meaning | Default |
|---|---|---|
| `I7_REMINDER_FIRST_DAYS` | days an approver waits before the first reminder (0 = no reminders) | 0 |
| `I7_REMINDER_REPEAT_DAYS` | days between later reminders | 1 |
| `I7_REMINDER_MAX` | how many reminders in all | 3 |
| `I7_ESCALATE_AFTER_DAYS` | days before it escalates (0 = never) | 0 |
| `I7_ESCALATION_ROLE` | Entra role told on escalation, e.g. `Spares.PlantHead` | blank |

Escalation needs both the days and the role. The role must have `notification_recipients` rows for each plant, or the
escalation is recorded as a failed notification. Example (first reminder after 1 day, daily, at most 3, escalate at
3 days): `I7_REMINDER_FIRST_DAYS=1`, `I7_REMINDER_REPEAT_DAYS=1`, `I7_REMINDER_MAX=3`, `I7_ESCALATE_AFTER_DAYS=3`,
`I7_ESCALATION_ROLE=Spares.PlantHead`. A settings change restarts the app.

**Option B -- `notification_sla` rows, in minutes, for finer control** (a different timing per route or step). A row
always wins over the settings for the steps it covers:

```
POST /api/notifications/admin/sla                      (Spares.Admin only)
{ "approval_route": "ALL", "first_reminder_minutes": <n>, "repeat_minutes": <n>, "max_reminders": <n>,
  "escalate_after_minutes": <n>, "escalation_role": "Spares.PlantHead" }
POST /api/notifications/admin/sla/{id}/update          -- change, or switch off with "active": false
```

`approval_route` is `OAR`, `ROP_MAX` or `ALL`; add `role` (an Entra role value) for a timing that applies to one approval
step only. The most specific row wins. The business-approved values are **not** set anywhere in code.

### Email through Microsoft Graph (when ready)

Delivery is off until all of these are done. `NOTIFICATION_EMAIL_PROVIDER` stays `logging` until then.

- [ ] Tenant admin consent for **Mail.Send (application)** on an app registration (Neil).
- [ ] A shared mailbox to send from. Restrict the app to that mailbox with an Exchange application access policy.
- [ ] Set `NOTIFICATION_EMAIL_PROVIDER=graph`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`
  (from Key Vault, never the repo) and `GRAPH_SENDER_MAILBOX`.
- [ ] Set `NOTIFICATION_FRONTEND_BASE_URL` (e.g. `https://spares.example.com`). Every email carries a link built from it;
  the Graph adapter refuses to send an email without one.
- [ ] Send a real test and check the `notification` row went `PENDING` -> `SENT`. Until you have seen that, email delivery
  is **unverified**. If Graph is selected with a setting missing, every email fails with a message naming the setting.

Links open the normal review page and require the normal sign-in: an unauthenticated visitor is sent to the login page
and returned to the page afterwards (`src/proxy.ts`). There is no approving from an email link.

### I13 nightly exception detection (off until you turn it on)

`NOTIFICATION_I13_DETECTION_ENABLED` defaults to **`false`**. Without it, detection runs only after an assistant
session (for that one material and plant) or through `POST /api/i13/act/run/detect`, so the Exceptions screen fell
behind the data: a plan that went overdue was not raised until somebody happened to run detection for its material.

When on, the `exception_detection` job runs detection over every material once a day, in the first tick at or after
`NOTIFICATION_I13_DETECTION_HOUR_UTC` (default 1). It reads the I13 snapshot exactly as the manual endpoint does, and
skips (trying again next tick) while there is none. It runs before `requester_escalations`, so an exception raised
or resolved tonight is seen by escalation in the same tick. A second run on the same day -- after a restart, or on a
second instance -- changes nothing: detection only creates what is missing, keeps what exists (no second
notification) and resolves what cleared.

The dashboard's Planned Overdue and Non-AI cards and the summary report count the same queue (unresolved exceptions
only), so they match the Exceptions screen once detection is current.

**Before turning it on:** the first run raises a Non-AI exception for every OAR reservation whose SAP item text has no
assistant reference -- on non-prod about 29,000 more than the queue holds today, most from before the assistant
existed -- and notifies each requester it can resolve.

### I13 automatic escalation (off until you turn it on)

`NOTIFICATION_I13_ESCALATION_ENABLED` defaults to **`false`** and is separate from the scheduler switch. Turning the
scheduler on does not start it. When on, each run:

- needs `I13_HOD_RECIPIENTS` (otherwise it skips and logs why),
- only takes exceptions at plants that have a HOD, so nothing is moved to "escalated" with nobody to tell,
- takes at most `NOTIFICATION_I13_ESCALATION_BATCH_SIZE` (default 100), the oldest overdue first, so a backlog is worked
  through over several runs (a full batch is logged),
- escalates each one by exactly the existing `process_escalations` rules (`requester_response_days` is unchanged).

A batch that fails is logged and saved nothing. The manual `POST /api/i13/act/run/escalate` is unchanged and unbounded.
Before enabling, count the exceptions that are overdue so you know how large the first batches will be.

### I13 requester and HOD

When an OAR exception is detected and has a requester, that person gets a **Confirm Exception** notification (in-app
and email) with the material, plant, reason and exception code, asking: "Why was this material not consumed as planned,
in your own words." Its link opens the Procurement Exceptions page narrowed to that material and plant
(`/oar-utilization/aging-exceptions?material=...&plant=...`). One notification per exception per person: the
idempotency key stops a repeat however often detection runs.

If the requester has not confirmed or explained within `I13_REQUESTER_RESPONSE_DAYS` (default **5**, counted from when
the exception was routed to them), the next escalation run moves it to escalated and tells the plant's HOD: "This
exception is still pending: the requester has not confirmed it or given an explanation", with the details, who the
requester is, when it was detected and how many days ago. It is sent once. An exception the requester has already
confirmed is never escalated, and an escalated one is not escalated again.

Required production configuration (nothing is set for you):

- [ ] `I13_REQUESTER_RECIPIENTS` -- `SAP requester name:Entra object id,...` (for example `Darren:<object id>`). The
  requester on an exception is a SAP name, but a person can only be notified by object id. A requester who is not
  listed and not already an object id is recorded as a **failed** notification ("invalid identity"), never guessed.
  Check the failed `notification` rows after the first detection run to see which names still need mapping. Matching
  ignores case.
- [ ] `I13_HOD_RECIPIENTS` -- `PLANT:Entra object id,...`, one HOD per plant (for example `1300:<id>,1500:<id>`). A plant
  with no entry is left alone, not moved to escalated.
- [ ] `NOTIFICATION_I13_ESCALATION_ENABLED=true` -- off by default and separate from the scheduler. Also leave
  `NOTIFICATION_SCHEDULER_ENABLED=true`. `NOTIFICATION_I13_ESCALATION_BATCH_SIZE` (default 100) caps each run.
- [ ] `I13_REQUESTER_RESPONSE_DAYS` -- only if five days is not what you want.
- [ ] Each person needs an email on record (they have signed in) or `NOTIFICATION_FALLBACK_EMAIL`, or the email part
  is recorded as failed. The in-app notification does not depend on it.
- [ ] Email only leaves the system with `NOTIFICATION_EMAIL_PROVIDER=graph` and its settings (see the Graph section).

### Scheduler

| Setting | Default | Notes |
| --- | --- | --- |
| `NOTIFICATION_SCHEDULER_ENABLED` | `true` | Needs `DATABASE_URL`. Does not start I13 escalation. |
| `NOTIFICATION_SCHEDULER_INTERVAL_SECONDS` | `60` | Minimum 5. |
| `NOTIFICATION_RETRY_MAX` | `3` | Attempts for a failed email. |
| `NOTIFICATION_RETRY_INTERVAL_SECONDS` | `300` | Gap between attempts. |

One thread, started with the backend and stopped with it. Each tick runs, in order: approval reminders, unanswered I13
exceptions (escalate to the HOD), then email delivery. Each job takes a SQL Server application lock
(`spares_ai_notif_<job>`, no waiting) so only one worker runs it at a time. The lock only stops overlapping runs;
duplicates are prevented by each notification's idempotency key and by `notification_reminder_state`.

## Known limits

- Approval **hold** is not reminded about on purpose: it is a deliberate pause.
- I07 write routes still do not check `actor_role` against the token's roles (see "Who is acting").
- Local runs cannot reach Azure SQL, so the `sp_getapplock` path and real email delivery are not exercised by the test
  suite (it uses SQLite, a fake lock and a mocked Graph call).
