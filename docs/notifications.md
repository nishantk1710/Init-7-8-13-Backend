# Shared notifications

One notification service for approvals, repairable-spares exceptions and OAR exceptions. It lives in
`app/integrations/notifications/`; nothing else builds its own client.

## How it works

1. **Something happens** -- an approval moves (`app/initiatives/i7/notifications.py`), an exception is raised
   (I08, `app/initiatives/i8/notifications.py`) or goes unanswered (I13, `app/initiatives/i13/act/service.py`).
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
- [ ] Optional: `I8_NOTIFICATION_RECIPIENT_ROLES` -- `OVERDUE_REPAIR:Spares.Stores,MISSING_ATTESTATION:Spares.Stores,...`
  Blank means no one is told about repairable-spares exceptions through the shared service (the existing exception log
  is unaffected). These roles also need `notification_recipients` rows.
- [ ] I13 requester and HOD notifications need the identity to be an Entra object id (the HOD comes from
  `I13_HOD_RECIPIENTS`, `PLANT:identity`). An email address or SAP user name is refused and recorded as a failed
  notification. The recipient needs an email on record (or `NOTIFICATION_FALLBACK_EMAIL`) to be emailed.

### Reminders and escalation (off until you set them)

With no active row in `notification_sla` **nothing is reminded or escalated**. The notices sent when an approval moves
are not affected.

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

### I08 recovery

The I08 notification log and the shared notifications are saved in the same transaction. If queuing the shared
notification fails, the log row is kept and the next read of the exception queue queues exactly the logged items that
have no shared notification yet (current items of a configured type only, at most 200 per read). This also picks up
items logged while `I8_NOTIFICATION_RECIPIENT_ROLES` was empty, once it is set. Each notification has an idempotency
key, so repeated reads never duplicate. Turning roles on after a long backlog will queue the whole backlog, 200 reads
at a time.

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
