# Claude Code Prompt — Spares AI Platform (Next.js Mockup)

> Copy everything below this line into Claude Code.

---

Build a Next.js 14 (App Router) mockup for **"Spares AI"** — Vedanta's AI-driven procurement platform for alternate part and supplier recommendation in mining/heavy-industry operations. This is a fully interactive UI mockup with mock data. No real backend yet.

**Tech stack:** Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui, lucide-react, recharts.

---

## CRITICAL DESIGN CONCEPT

**The chat IS the interface.** This is NOT a traditional dashboard with separate pages for search, results, approvals, etc. Instead, every procurement interaction happens inside a chat session. The AI asks structured questions via selectable option cards (not free text), and each user selection becomes a traceability parameter logged for audit. The chat simultaneously serves as: the procurement action, the approval trigger, and the audit record.

Think of it as: ChatGPT meets SAP procurement, purpose-built for mining engineers and procurement officers who need to find alternate parts for industrial equipment.

---

## LAYOUT — Three-Panel (Desktop-First)

The main layout is a three-column grid that fills the viewport:

```
┌─────────────┬──────────────────────────────┬─────────────────┐
│  SIDEBAR    │       CHAT AREA              │  RIGHT PANEL    │
│  (240px)    │       (flex-1)               │  (260px)        │
│             │                              │                 │
│ • Logo      │ ┌──────────────────────────┐ │ [Workflow|Emails│
│ • Active    │ │ Chat header + session ID │ │  |Trace] tabs   │
│   sessions  │ ├──────────────────────────┤ │                 │
│ • Quick     │ │                          │ │ Approval steps  │
│   actions   │ │  Chat messages with      │ │ (6-step tracker)│
│ • Category  │ │  AI option cards         │ │                 │
│   filters   │ │  Price comparison cards  │ │ Email notifs    │
│             │ │  Market benchmarks       │ │ (sent/pending/  │
│             │ │                          │ │  escalated)     │
│             │ ├──────────────────────────┤ │                 │
│             │ │ Input bar + send button  │ │ Traceability    │
│             │ └──────────────────────────┘ │ log + tags      │
└─────────────┴──────────────────────────────┴─────────────────┘
```

---

## SIDEBAR (Left)

### Header
- Logo: CPU icon + "Spares AI" title
- Subtitle: "Vedanta procurement platform"

### Active Sessions (each is a chat)
Show 3-4 active chat sessions. Each shows:
- Chat icon
- Part/equipment short name (e.g., "Pump seal — Milling")
- Status subtitle (e.g., "3 alternates found", "Pending approval", "Escalated")
- Badge indicators: count badge for pending, red "!" badge for escalated
- The current session is highlighted with accent background

### Quick Actions
- Search materials
- Price benchmarks
- Pending approvals (with count badge showing "4")
- Audit trail

### Categories (plant/process areas)
- Flotation
- Conveyance
- Milling
- Instrumentation

Each category has a distinct icon.

---

## CHAT AREA (Center) — The Core

### Chat Header
- Green status dot (AI online)
- Session title: "Pump seal — Milling unit 3"
- Session ID: "Session #SPR-2847" (right-aligned, muted)
- Options menu icon

### Chat Body — Conversation Flow

Build a realistic multi-turn conversation that demonstrates the full flow. Each message is either `user` (right-aligned, accent background) or `ai` (left-aligned, surface background with border).

**Turn 1 — User Request:**
> "I need an alternate supplier for material 500-14892 — mechanical seal for the milling pump"

**Turn 2 — AI Identifies Material + Asks Context:**
AI says: Found material **500-14892** — "Seal Assy, Mech Type XR-200, Flowserve". Then asks **"Which equipment is this installed on?"** with selectable option cards:

Option cards (radio-style, one selectable at a time):
1. ⚙️ **Warman 8/6 AH slurry pump** — "Milling circuit — primary grind" ← pre-selected
2. ⚙️ **Metso HM150 pump** — "Milling circuit — cyclone feed"
3. ❓ **Not sure / other equipment** — "I'll describe the application manually"

Each option card has: icon, bold label, description, radio circle indicator.
Footer: "Spares AI · 10:23 AM · *Selection logged for traceability*"

**Turn 3 — User Confirms:**
> "Warman 8/6 AH slurry pump"

**Turn 4 — AI Confirms Specs + Asks Match Tier:**
AI says: Based on the Warman 8/6 AH application, matched specs: **shaft size 65mm, seal face SiC/SiC, elastomer Viton, rated to 12 bar**. Found **3 alternate options**. Asks **"What match tier are you looking for?"**

Option cards:
1. 📋 **Direct equivalent** — "Same OEM part from different distributor — light approval" ← pre-selected
2. 🔧 **Technical equivalent** — "Different manufacturer, same specs — engineering sign-off"
3. 💡 **Show all tiers** — "See all 3 options with price comparison"

Footer: "Spares AI · 10:24 AM · *Tier selection determines approval workflow*"

**Turn 5 — AI Shows Recommendation (Rich Card):**
This is the hero content. Render a styled comparison card (NOT a chat bubble — a structured result block):

```
┌─────────────────────────────────────────────────┐
│ ✓ Direct equivalent found                       │
│ ┌─────────────────┬──────────────────────────┐  │
│ │ Current supplier│ Alternate — direct equiv  │  │
│ │ Flowserve SA    │ Bearings Int'l (Pty)      │  │
│ │ XR-200-65-VV   │ FLS-XR200-65V             │  │
│ │ R 48,200 (red)  │ R 38,500 (green)          │  │
│ │ Last PO: 14 Mar │ 20% below current ↓       │  │
│ └─────────────────┴──────────────────────────┘  │
│ ┌───────────────────────────────────────────┐   │
│ │ ℹ Market index benchmark:                 │   │
│ │   R 36,800 – R 42,100 for this spec.      │   │
│ │   Alternate is within range.               │   │
│ └───────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

The "Current supplier" card uses default styling, the "Alternate" card has an accent border highlight. Prices: current in red, alternate in green. Market benchmark shown in an info banner below.

**Below the comparison card — Action Option Cards:**
1. 🚀 **Proceed with alternate** (green accent) — "Triggers approval workflow and notifies stakeholders"
2. 👁️ **View technical equivalents too** — "See options from other manufacturers"
3. 📄 **Export comparison report** — "PDF for offline review with engineering"

### Chat Input Bar
- Text input: "Ask about materials, alternates, or pricing..."
- Send button (accent color, send icon)

---

## RIGHT PANEL — Context & Workflow

### Tab Bar
Three tabs: **Workflow** (active) | **Emails** | **Trace**

### Workflow Tab — Approval Tracker
A vertical stepper showing 6 steps:
1. ✅ **Material identified** — "Auto · 10:23" (done, green)
2. ✅ **Application confirmed** — "User · 10:24" (done, green)
3. 🔵 **Alternate selection** — "Awaiting user" (active, accent blue)
4. ⬜ **Procurement approval** (pending, muted)
5. ⬜ **Engineering sign-off** (pending, muted)
6. ⬜ **PO generation** (pending, muted)

Done steps have checkmark icons. Active step is highlighted. Pending steps are dimmed with number.

### Emails Tab — Notification Tracker
Show 3 notification items:
1. 📧 **Sent** (accent) — "Session started alert sent to procurement team" — 10:23 AM
2. 🕐 **Pending** (warning/amber) — "Approval reminder queued for R. Patel (Procurement head)" — "Triggers on selection"
3. ⚠️ **Escalated** (danger/red) — "Auto-escalation to VP Supply Chain if no response in 24h" — "Configured"

Each has a colored icon circle, text, and timestamp.

### Trace Tab — Traceability Log
Category tags as colored pills:
- "Milling" (accent blue)
- "Direct equiv." (green)
- "In progress" (amber/warning)

Below tags, a structured info block:
- **Material:** 500-14892
- **Equipment:** Warman 8/6 AH
- **Requester:** M. Naidoo
- **Spec match:** 65mm / SiC / Viton
- **Selections:** 2 of 6 steps

---

## ADDITIONAL PAGES (Secondary)

### `/dashboard` — Overview
- Summary cards: Active Sessions, Alternates Found This Month, Cost Savings (ZAR), Pending Approvals
- Recent sessions table (session ID, part, requester, status, date)
- Cost savings trend chart (recharts, last 6 months in ZAR)
- Category breakdown pie/donut chart

### `/materials` — Material Search
- Search bar with filters: Category, Manufacturer, Lifecycle Status (Active/EOL/Obsolete)
- Results table: Material Code, Description, Manufacturer Part No., Last Vendor, Category, Last PO Price (ZAR), Stock Level
- Click a material row → opens a new chat session pre-filled with that material

### `/approvals` — Pending Approvals Queue
- Table of pending approval items across all sessions
- Columns: Session ID, Material, Requester, Match Tier, Savings %, Waiting Since, Approver
- Action buttons: Approve, Reject, Escalate
- Filtering by: Category, Match Tier, Urgency

### `/audit` — Audit Trail
- Full traceability log across all sessions
- Columns: Timestamp, Session ID, Action, Actor (User/AI/System), Material, Detail
- Expandable rows showing full context of each decision point
- Export to CSV button

---

## MOCK DATA (`/lib/mock-data.ts`)

Generate realistic mining/heavy-industry spare parts data:

**Materials (20+):**
- Mechanical seals (Flowserve, John Crane, EagleBurgmann)
- Slurry pump impellers (Warman, Metso, KSB)
- Conveyor belts and rollers (Continental, Fenner, Dunlop)
- Pressure transmitters (Endress+Hauser, Yokogawa, ABB)
- Bearings (SKF, NSK, Timken, FAG)
- Valves (Fisher, Neles, Weir)
- Electrical motors (WEG, Siemens, ABB)

Include: material code (format: 500-XXXXX), description, manufacturer, manufacturer part number, specs object, category, lifecycle status, last PO price (ZAR — use R prefix, range R200 – R250,000), last vendor, stock level, lead time days.

**Suppliers (10+):**
Use realistic South African / international industrial distributor names:
- Bearings International (Pty) Ltd
- BMG (Bearing Man Group)
- Becker Mining South Africa
- Hytec Group
- SEW-Eurodrive SA
- Zest WEG Group
- Invicta Holdings
- Motion Control Systems
- FlowserveZA
- Weir Minerals Africa

Include: name, region, categories served, rating (1-5), on-time delivery %, avg lead time, certifications (ISO 9001, ISO 14001, BBBEE Level).

**Chat Sessions (5+):**
Pre-built conversation flows with different states:
1. Pump seal — Milling (in-progress, at alternate selection step) ← the main demo
2. Conveyor bearing — Conveyance (pending procurement approval)
3. Pressure transmitter — Instrumentation (escalated — no response 24h)
4. Impeller — Flotation (completed, PO generated)
5. Control valve — Milling (new session, material identification step)

**Alternate Recommendations:**
For each material, 2-4 alternates with:
- Match tier: "Direct equivalent", "Technical equivalent", "Functional alternative"
- Match confidence: 75%-99%
- Alternate part number, manufacturer, supplier
- Price (ZAR), MOQ, lead time
- Spec comparison (which specs match, which differ)
- Market benchmark price range

---

## CURRENCY & LOCALE

- All prices in South African Rand: display as "R 38,500" (R prefix, space, number with comma thousands)
- Date format: DD MMM YYYY (e.g., "14 Mar 2026")
- Time format: 12-hour with AM/PM

---

## DESIGN SYSTEM

**Color palette:**
- Primary/Accent: Indigo-blue (for active states, selections, highlights)
- Success/Good: #1D9E75 (green — for matched specs, savings, done steps)
- Warning/Caution: Amber/orange (for pending states, close-match specs)
- Danger/Mismatch: Red (for escalations, price increases, spec mismatches)
- Surfaces: Use shadcn/ui default light/dark theme tokens

**Typography:**
- Use Inter or system font stack
- Chat messages: 13px, line-height 1.55
- Labels/badges: 11px, uppercase, letter-spacing 0.5px
- Data values: 16px bold for key numbers (prices)

**Component patterns:**
- Option cards: bordered cards with icon + label + description + radio indicator. On hover: accent border. On selected: accent background + filled radio dot.
- Status badges: colored pill shapes (11px, rounded)
- Workflow stepper: vertical with circle icons (checkmark for done, number for pending, accent dot for active)
- Comparison cards: side-by-side grid with border highlight on recommended option
- Info banners: subtle accent background with info icon for benchmark data

**Dark mode:** Support both. Default to system preference.

---

## PROJECT STRUCTURE

```
src/
  app/
    layout.tsx                    # Root layout — three-panel grid
    page.tsx                      # Redirect to /chat or show dashboard
    chat/
      page.tsx                    # Main chat view (default session)
      [sessionId]/page.tsx        # Specific session chat
    dashboard/page.tsx
    materials/page.tsx
    approvals/page.tsx
    audit/page.tsx
  components/
    layout/
      sidebar.tsx                 # Left sidebar navigation
      right-panel.tsx             # Right context panel (workflow/emails/trace)
      panel-tabs.tsx              # Tab switcher for right panel
    chat/
      chat-header.tsx
      chat-body.tsx
      chat-message.tsx            # Individual message (user or AI)
      chat-input.tsx
      option-card.tsx             # Selectable option with radio indicator
      option-group.tsx            # Group of option cards
      comparison-card.tsx         # Side-by-side price/spec comparison
      market-benchmark.tsx        # Info banner for market price range
      action-options.tsx          # Post-recommendation action cards
    workflow/
      approval-stepper.tsx        # Vertical 6-step workflow tracker
      email-tracker.tsx           # Email notification list
      traceability-log.tsx        # Tags + structured info block
    dashboard/
      summary-cards.tsx
      sessions-table.tsx
      savings-chart.tsx
    materials/
      material-search.tsx
      material-table.tsx
    approvals/
      approvals-table.tsx
    audit/
      audit-log.tsx
    shared/
      status-badge.tsx            # Colored pill badges
      category-tag.tsx
      price-display.tsx           # R-prefixed ZAR formatter
  lib/
    mock-data.ts                  # All mock data
    types.ts                      # TypeScript interfaces
    utils.ts                      # Formatters (currency, date, etc.)
    constants.ts                  # Categories, statuses, workflow steps
```

---

## BUILD ORDER

1. **First:** Scaffold Next.js project, install all deps (shadcn/ui, lucide-react, recharts, @tanstack/react-table), create types.ts and mock-data.ts
2. **Second:** Build the three-panel root layout (sidebar + chat area + right panel)
3. **Third:** Build the chat page with the full conversation flow — this is the hero. Make the option cards interactive (clickable, toggle selection). Make the comparison card pixel-perfect.
4. **Fourth:** Build the right panel tabs (workflow stepper, email tracker, traceability log)
5. **Fifth:** Build secondary pages (dashboard, materials, approvals, audit)
6. **Sixth:** Add dark mode toggle, loading skeletons, toast notifications for actions

**Priority:** The `/chat` page with the three-panel layout is 80% of the product. Make it exceptional. Secondary pages are supporting context.
