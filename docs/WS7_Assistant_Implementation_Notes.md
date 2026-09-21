# WS7 — Reservation-Time Assistant: what was built

**Branch:** `feat/vp/ws7-assistant` (off `feat/vp/init8`)
**Date:** 21 September 2026
**Plan this follows:** `docs/WS7_Chat_Assistant_Backend_Plan.md`
**Covers:** W7.1 – W7.4, W7.6 (backend half), plus I08 FR-5/6/7 and I13 FR-2/3/4

---

## 1. The short version

The plan said both halves of WS7 were finished and the chat in the middle was
missing. That was right. This branch builds the middle.

A planner starts a reservation in SAP. The assistant opens, works out whether
the part is a repairable (Initiative 08) or planned-on-demand (Initiative 13),
tells them what they need to know, asks the questions that flow needs, and hands
back a 10-character reference to type into SAP.

Everything in the plan's build order is done except the parts that need SAP or
the frontend. Those are listed in section 7.

---

## 2. What now works, end to end

Both flows run against the real seeded data.

**Initiative 08 — repairable spares.** Ask for a reservation on an 80-series
part, and the assistant says whether a repairable unit already exists: on the
shelf, or on an open repair order with a due date. If one does, it asks whether
they still want to buy new. If they say yes, it records why.

**Initiative 13 — planned on demand.** Ask for an OAR part, and the assistant
shows stock, what is already on order, months of cover, whether the part has
moved recently, and what other plants hold. Then it captures the consumption
plan, suggests a quantity, and records the reason if the planner keeps more.

A real session from the local database:

```
POST /api/assistant/sessions   { materialId: 8000004665, plant: 1300 }
  -> flow i08, session SFDR49BSHH
  -> "There is 1 open repair for 1 unit, the earliest due back 2025-04-24
      (1 of them already past the promised date). That repair was due 515 days
      ago and still has not arrived, so its date is no longer a forecast."

POST .../turns  { choice: proceed_new }        -> asks for a justification
POST .../turns  { reason_category, free_text } -> done, hands back the reference

GET  /api/assistant/sessions/SFDR49BSHH
  -> outcome COMPLETED, 3 turns, 1 justification, the advice as it was served
```

### New endpoints

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/api/assistant/sessions` | Open the assistant. Routes the material, mints a session if there is anything to say. |
| POST | `/api/assistant/sessions/{id}/turns` | Answer the current question, get the next one. |
| GET | `/api/assistant/sessions/{id}` | The full trace: advice served, plan, suggestion, justifications. |
| GET | `/api/assistant/sessions` | The session log. |
| POST | `/api/assistant/ask` | The free-text box. Fixed set of questions, no AI. |
| GET | `/api/assistant/ask/suggestions` | What the free-text box can answer. |
| POST/GET | `/api/justifications` | Reason + free text. Shared by both initiatives. |
| POST/GET | `/api/i13/consumption-plans` | Capture a plan. The write path `plans.py` never had. |
| GET | `/api/i13/quantity-suggestion` | FR-3 on its own, without a conversation. |
| GET | `/api/i8/repairable-unit` | FR-6 on its own, without a conversation. |

### New tables

All five are **append-only**. Postgres triggers block UPDATE and DELETE, the
same way `i8_attestation` already does.

| Table | Holds |
| --- | --- |
| `assistant_session` | One row per invocation, plus the advice exactly as it was served. |
| `assistant_turn` | Each question asked and answer given. |
| `justification` | Why somebody went ahead anyway. Both initiatives, one table. |
| `consumption_plan` | The real plans. Replaces the 742 fabricated CSV rows. |
| `quantity_suggestion` | What we suggested, what they kept, and the arithmetic behind it. |

---

## 3. Decisions, and what was done about them

The plan asked eight questions before any code was written. These are the
answers as built. **All of them are reversible cheaply except the two marked
otherwise.**

### One session table, not one per initiative (§4.1)

One table, one ID namespace, a `flow` column saying `i08` or `i13`.

The reason is simple: SAP gives us **one field on the reservation** to put the
reference in. If there were two tables, a reference coming back off a
reservation could not be looked up without guessing which one to look in.

Initiative 13 had already assumed this. Its `NoPlanReason.SESSION_WITHOUT_PLAN`
exists, unused, with a comment saying it is "for a future CAPTURE source that
tracks sessions independently of plans (e.g. a reservation-time chatbot session
store)". This is that store.

**Hard to reverse** — the table is append-only.

### The conversation is server-driven (§4.2)

The backend decides what to ask next; the frontend just renders it.

The mock-up did it all in the browser. That would have meant an abandoned
session left no record, and both FRSs want "advice given, not acted on" counted.
It would also have meant the script existing twice, in Python and TypeScript,
drifting apart until they disagreed about a compliance question in front of a
user.

One consequence worth knowing: **there is no "current step" stored anywhere.**
The next question is worked out from the answers so far, every time. That sounds
wasteful and is not — it means the conversation and its audit trail are the same
thing and cannot disagree.

### One shared endpoint prefix (§4.3)

`/api/assistant/*`. The SAP pop-up knows a material and a plant. It cannot know
whether that is an 80-series or an OAR part — that is the thing we are working
out — so it cannot pick between `/api/i8` and `/api/i13`.

### The numbers are computed; the model only writes the sentence (§4.4)

Every number the assistant states is already a field we hold. Nothing needs to
be inferred, so nothing is asked of a model.

The optional narrative layer is built and is **off by default**
(`ASSISTANT_NARRATIVE_ENABLED=false`). This is still a visible deviation from
both FRSs, which say "responses generated through the provider-agnostic LLM
layer", and it needs sign-off before anything depends on it.

Two safety rules are in the code: a provider failure silently falls back to the
deterministic sentence, and the stub provider's placeholder text is refused by
name so it can never be shown to a planner as advice.

### The session reference format (§4.5)

`S` + 8 characters + 1 check character = **10 characters**, which is what
`Bednr` holds. The alphabet is Crockford base32, which drops `I`, `L`, `O` and
`U` so nothing is confusable. Typing `O` instead of `0` is corrected silently —
the planner read it right, the font was wrong.

Example: `SFDR49BSHH`.

The check character means a mistyped reference **fails immediately** instead of
matching nothing. That matters more than it sounds: "no such session" is the
exact compliance finding I13 raises for somebody who skipped the assistant, so
without a checksum a typo would become a false accusation against somebody who
did everything right.

The placeholder in the CSV today is `SESS-000001` — eleven characters. It does
not fit the field it stands in for. Nothing depended on it yet.

**Hard to reverse** — references go into an append-only table.

### Free text answers a fixed list, not anything (§4.6)

The free-text box works, and answers three questions from real data: overdue
repairs, OAR exceptions, and what is waiting on somebody. **No AI is involved**
— a test asserts the module does not even import the AI layer.

Anything else gets a plain "I cannot answer that, here is what I can". General
question-answering over the whole dataset is still a separate piece of work that
has not been scoped, and every refusal says so.

### The consumption plan captures everything and is read narrowly (§6)

This was option 2 of the three the plan set out.

A captured plan holds the full FRS record: a planned **window** (not a single
date), a cost centre and a work order where known. Today's exception engine
still reads only the window's start date, exactly as it read the CSV's single
date — so **detection behaviour has not changed**.

Widening the reader changes when breaches fire, and that deserves its own change
with its own tests rather than arriving as a side effect of building the chat.

The alternative — capturing only what the engine reads today — would have been
quicker and would have permanently under-captured. The table is append-only, so
those fields could never have been added later.

---

## 4. Problems found along the way

These are all real, all found by running the code rather than reading it, and
all fixed on this branch unless stated.

### `alembic upgrade head` was broken, and the local database was stale

The I08/I13 merge left the migration history with **two heads**. Alembic refuses
to run `upgrade head` when that is true:

```
Multiple head revisions are present for given argument 'head'
```

So nobody could bring a database up to date on `feat/vp/init8`. The local
Postgres was still sitting on the I08 migration and had **never received any of
the four I13 migrations** — the WATCH mart, consumption attribution,
reclassification and ACT exception tables did not exist.

The WS7 migration declares both heads as its parents, which fixes it. Running it
applied the four missing I13 migrations on the way through.

### The "wait or buy" advice was backwards for overdue repairs

This is the worst bug found, and it was found by running a real session.

An overdue repair has a due date in the **past**. Comparing that date against a
delivery lead time finds it "sooner", so the assistant recommended waiting — for
a unit that was already 515 days late with no new forecast.

**695 of the 788 open repair lines in this extract are overdue.** So the naive
answer would have been wrong on 88% of them, and wrong in the direction that
costs money and keeps a machine down.

It now returns "cannot say" and tells the planner the date is no longer a
forecast and should be chased. Pinned by tests so it cannot come back.

### Numbers were being shown unrounded

The assistant told a planner they had

```
5.29498153846153846153846154 months of cover
```

Three modules had each written their own number formatter and all three had the
same bug — they stripped trailing zeros but never rounded. Fixed once, in
`app/shared/numbers.py`.

Note that **only the display rounds**. Stored records keep full precision,
because rounding evidence loses information an append-only table cannot get
back.

### Cross-plant stock listed plants holding nothing

The assistant said "other plants hold 0 at 1100, 0 at 1200" — which announces
that there is stock elsewhere and then says there is not. Now filtered.

### Captured plans were being written with a status the engine rejects

Found by the step-8 test, and this is exactly the class of thing that test
exists for.

Plans were being saved with `status="ACTIVE"`. The exception engine checks
`plan.status != "OPEN"` before treating a plan as a live commitment, and the
reference CSV uses `OPEN` / `CLOSED`.

So a planner could fill in the whole form, and the engine would still record
"no valid plan" — because one word did not match. It is now `OPEN`, and there
is a test asserting it bluntly.

### The write-path guarantee had quietly stopped being true

`tests/test_i8_api.py` asserts Initiative 08 has exactly one write path. It
still passes, and it had stopped describing the application: it only looks at
`/api/i8`, and the app already had **five** write endpoints across the PR event
listener and the ACT routes. Nobody noticed because nothing was watching.

Rather than loosen or rename it, the guarantee is now restated app-wide in
`tests/test_write_paths.py`. Every non-GET route must be listed with a sentence
saying what it writes and why. Adding an endpoint fails the suite until somebody
writes that sentence.

It earned its place immediately — it caught two ACT route paths I had guessed
wrong.

### The checksum was weaker than intended

The first version of the session reference weighted characters 1, 2, 3… and only
checked the payload. That left the check character itself unverified, so
swapping it with its neighbour went undetected. Its own test caught it.

Now every character is weighted with an odd number, which catches **every**
single-character typo and every adjacent swap except characters exactly 16 apart
in the alphabet (about 3% of swaps). That limit is documented rather than
claimed away — 32 is not a prime number and no scheme of this kind can close it.

---

## 5. Step 8: does the chat actually drive the exception engine?

This was the plan's step 8, and it was worth doing early.

Until this branch, Initiative 13's exception engine had only ever been validated
against 742 plans a generator wrote. A plan captured through the chat is the
first input it has seen that it did not produce itself.

**What is proved.** A plan captured through the conversation is read back by the
engine's own plan reader, carries `source = CAPTURED` so it can be told apart
from the fabricated rows, and — run through the engine's own validity rule —
comes back as a valid plan. Detection runs cleanly with captured plans present.

**What is not proved, and cannot be yet.** The engine matches a plan to a
reservation by reservation number and item:

```python
plan = plan_by_reservation.get((entry.reservation_number, entry.reservation_item))
```

A plan captured through the chat has **no reservation number**, because the
reservation did not exist when the plan was given — the planner is still
creating it. So a captured plan cannot yet clear a `NO_PLAN` exception against
an existing reservation.

**This is blocker B2 in its concrete form.** The link is made by reading the
session reference back off the reservation, which needs `Bednr` exposed on
`ReservationItemSet`. Nothing on our side can close it.

There is a test that asserts the reservation number is still empty. When B2
lands, that test fails — which is the right moment for somebody to come back and
extend step 8 to prove the exception actually clears.

---

## 6. Two measurements worth knowing

Both were taken from the seeded database, and both differ from what the plan
assumed.

### Nearly every repairable is also OAR

**2,294 of the 2,350 80-series material-plant rows in MARC — 97.6% — are also
OAR** by MRP type.

The plan treated the overlap as an edge case needing a tie-break. It is not an
edge case; it decides which flow essentially every repairable spare gets. I08
wins, which is the right way round (a repair that already exists can stop a
purchase; the I13 flow is about sizing a purchase that is going ahead). **The
losing match is recorded on every session** so the decision stays reviewable.

### The OAR rule selects far more than the plan expected

The plan quotes a frontend scan finding ND + PD = 46.4% of the catalogue, with
47% of rows having no MRP type at all.

On the seeded backend data:

| MRP type | Rows | Share |
| --- | --- | --- |
| PD | 36,914 | 81.3% |
| ND | 7,480 | 16.5% |
| VB | 950 | 2.1% |
| blank | 49 | 0.1% |
| V1 | 16 | 0.0% |

**ND + PD is 97.8%, and only 0.1% is blank.** These are different populations —
the frontend scanned a 2,183-row CPI sample, this is the 45,409-row seeded
extract — but the consequence is that the assistant will ask for a consumption
plan on almost the entire catalogue, not half of it.

This does not block anything; it is one config line. But open question 14 to VZI
is more urgent than the plan implies.

---

## 7. Not done, and why

| Item | Why |
| --- | --- |
| Reading the session reference back off a reservation (FR-8 / I13 FR-4 second half) | **Blocked — B2.** `Bednr` is not exposed on `ReservationItemSet`. Everything up to the link is built; the trace says so on every session rather than leaving it to be inferred. |
| Wiring the frontend chat to these endpoints (step 9) | Frontend repo, not this branch. The endpoints and their shapes are ready. |
| Exception-queue screen (step 10) | Frontend repo. |
| Widening the exception engine to read a planned *window* | Deliberate — see §6 of the plan. It changes when breaches fire and needs its own change and its own tests. |
| Email notifications | Out of scope in the plan. |

---

## 8. Things that will look odd in a demo, and are not broken

- **Every consumption plan on screen today is still fabricated** unless somebody
  captures one through the chat first. 742 rows came from a generator, with
  invented `SESS-000001` references. Plans now carry a `source` field saying
  `CAPTURED` or `REFERENCE_CSV`. **Say this out loud before demoing the
  exception queue** — the engine is real, most of its input is not.

- **The assistant never says "the vendor has it."** Zero of the 788 open repair
  lines carry a dispatch movement, so no open line can be confirmed as
  physically with the vendor. It says "a repair is on order and due back",
  which is true.

- **Every session is issued to `UNAUTHENTICATED_LOCAL_USER`.** Entra is not
  wired in. This is visible on purpose rather than hidden behind a blank field.

- **Most repair due dates are in the past.** The data is a frozen July 2026
  extract and 695 of 788 open repairs are overdue in it.

---

## 9. Still needs an answer

Unchanged from the plan, and now more concrete because the code exists:

1. **Session validity window.** Defaulted to 72 hours. Expiry is **reported and
   never enforced** — the reservation is already in SAP and we cannot write
   back, so treating an expired reference as non-compliant would raise an
   exception nobody could ever clear.
2. **Justification reason categories.** Seven placeholders are configured. VZI's
   own list is needed. Configuration, not an enum, so it is an `.env` change.
3. **The three FR-3 quantity values** — cover ceiling (12 months), look-back (12
   months), minimum history (3 consumptions). All ours. They travel with every
   suggestion so the number can be argued with.
4. **Is the OAR rule right?** See §5. It now selects 97.8% of the seeded
   catalogue.
5. **Does I08 beating I13 match the business intent** when a part is both? It
   applies to 97.6% of repairables.
6. **Sign-off on the deterministic-core deviation** (§4.4) before the narrative
   is switched on.

---

## 10. Test status

**234 new tests**, all passing.

Across the whole suite (excluding `tests/i13`'s slow Postgres tests, and
`test_seed.py` / `test_storage.py`, which cannot be collected because the
`azure` package is not installed in this environment):

```
764 passed, 15 failed
```

**All 15 failures are pre-existing and none of them are WS7's.** They were
checked by running the same files on `feat/vp/init8` in a separate worktree, and
the counts match exactly:

| File | Failing | On base branch too? |
| --- | --- | --- |
| `test_sap_contract.py` | 12 | Yes — 12 |
| `test_sap.py::TestFilterGuard` | 3 | Yes — 3 |

These are the SAP drift tests. `pytest.ini` already documents them: a discovery
re-run refreshed the snapshot and five `ZMM_KPI02_SRV` sets began rejecting
every request with HTTP 400. They assert facts about an external system we do
not control, and re-baselining them would erase the only signal that ~1.17M rows
of change-document data stopped being readable. Nothing on this branch touches
the SAP client, the drift snapshot or `known_conditions.py`.

---

## 11. How to run it

```bash
alembic upgrade head          # applies the WS7 tables AND the four I13 ones
uvicorn app.main:app --reload
```

Tests:

```bash
pytest tests/assistant tests/test_write_paths.py \
       tests/test_i8_repairable_unit.py tests/test_i8_reservation_assistant.py \
       tests/test_shared_numbers.py tests/i13/test_quantity.py \
       tests/i13/test_reservation_assistant.py
```

These run in a few seconds and need no database. The full `tests/i13` suite
takes about 17 minutes because of its Postgres-backed tests.

Relevant settings, all with working defaults:

```
ASSISTANT_SESSION_ID_LENGTH=10
ASSISTANT_SESSION_ID_PREFIX=S
ASSISTANT_SESSION_TTL_HOURS=72
ASSISTANT_NARRATIVE_ENABLED=false
ASSISTANT_JUSTIFICATION_REASON_CATEGORIES=...
ASSISTANT_FREE_TEXT_INTENTS_ENABLED=true
I13_QUANTITY_COVER_CEILING_MONTHS=12
I13_QUANTITY_LOOKBACK_MONTHS=12
I13_QUANTITY_MIN_HISTORY_CONSUMPTIONS=3
```
