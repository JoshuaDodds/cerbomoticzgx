# Mobile UX plan — iPhone 12 Pro (390 × 844)

Goal: make the cerbomoticzGx dashboard genuinely **usable and beautiful on a phone**,
using best‑in‑class mobile UX patterns — without regressing the desktop experience.

## Current status

This plan has been implemented as the phone breakpoint in
`frontend/static/css/app.mobile.css` with guarded mobile-only JavaScript in
`frontend/static/js/app.js`.

Current mobile behavior:

- Compact sticky top header plus swipeable status chips.
- Bottom navigation ordered **Menu · Flow · Schedule · Trends · Advisor**.
- Hamburger menu for Battery, Venus, Victron Schedule, Configuration, and Replan.
- Overview is the mobile entry point; Schedule/Trends/Advisor/Flow focus their main
  content and hide redundant overview cards.
- Schedule auto-scrolls to the currently expanded time slot.
- Battery/Venus embedded panes remain scrollable while scrollbar chrome is hidden.
- The Overview Solar card uses the optimizer-adjusted remaining PV as the primary
  value and keeps the raw source visible as `VRM forecast` subtext, matching desktop.

## Hard constraint (read first)

**The desktop rendering must not change at all.** The current desktop layout is the
locked baseline (see the visual reference captured in chat). Every mobile change is
**additive and scoped to a phone breakpoint** so it is physically impossible for it to
affect desktop:

- All new rules live inside `@media (max-width: 680px)` (phones; tablets handled later),
  in a clearly‑marked block — ideally a separate `static/css/app.mobile.css` loaded
  **after** `app.css`. Never edit an existing base rule; only override inside the media
  query.
- No HTML structure changes that are visible on desktop. Any new element (e.g. a bottom
  nav bar) ships `display: none` by default and is only shown inside the mobile media
  query.
- Any JS added for mobile (e.g. a card toggle, bottom‑nav wiring) must be a no‑op on
  desktop (guard on `matchMedia('(max-width: 680px)')` or attach handlers that don't
  alter desktop behaviour).
- **Regression gate:** before/after every change, view at ≥1024px and confirm it is
  pixel‑identical to the baseline screenshots. I can do this directly in Chrome
  (desktop screenshot diff) as part of each step. Breakpoint chosen at 680px so the
  desktop layout (which needs ~1024px) is never touched, with margin to spare.

## What's broken on mobile today (findings at 390px)

The app currently has **no phone breakpoint**, so the desktop layout is force‑fit:

1. **Sticky header explodes — the #1 problem.** `.topbar` is `position: sticky; top:0`
   and `.status-strip` is `flex-wrap: wrap`. At 390px the status chips (action, SoC,
   price, Today, Month, clock) wrap into a tall vertical stack, so the header becomes
   ~280–360px tall **and stays pinned** — it eats roughly half the viewport, and the
   brand logo, the ESS/Battery/Live nav, and the chips visually collide.
2. **Tab bar overflows.** The six tabs (Live · Trends · Schedule · Victron · Advisor ·
   Configuration) barely fit; "Configuration" clips at the edge. No wrap/scroll handling.
3. **Schedule table is unreadable.** The 8‑column CSS grid
   (`116px 1fr 82px 74px 64px 64px 88px 100px`, ~600px min) is crushed into 390px —
   columns overlap and the timeline + numbers are illegible.
4. **Configuration rows cramp.** The 3‑column `label | value | description` layout
   squeezes the description into a sliver.
5. **Day cost summary** rows justify import/export far apart, reading as loose, scattered
   numbers rather than a coherent block.
6. **Overview metric cards** (`.metrics`, `.overview-row`) don't reflow to a single
   column cleanly.
7. **Charts are the bright spot** — the SoC/price, monthly‑net, and gauge SVGs use a
   `viewBox`, so they scale and stay legible; they just want **more height** on mobile
   since width is the constraint.
8. **Power‑flow** scales (760×600 viewBox) but shrinks small — node text gets tiny on a
   narrow screen.
9. **Touch targets** (nav links, tabs, slot rows, editable config values) are below the
   44×44px iOS minimum.

## Design principles for the mobile build

- **Thumb‑first navigation.** Primary navigation belongs at the **bottom** on phones
  (reachable zone), not a tall top bar.
- **Progressive disclosure.** Show the one number that matters; tap to reveal detail.
  Phones are for glancing, not dense tables.
- **Cards over tables.** Replace multi‑column grids with stacked, tappable cards.
- **One column, generous rhythm.** Single‑column stacks, 16px gutters, ≥44px targets,
  larger base type.
- **Keep the data‑viz.** The charts already scale; lean into them — they're the most
  "app‑like" surface and should get more vertical space.
- **Native feel.** Respect safe areas (notch / home indicator via
  `env(safe-area-inset-*)`), momentum scrolling, and subtle motion.

## The plan, area by area

### 1. Header → compact, non‑exploding
- Shrink the brand to a small logo/wordmark; reduce `.brand-logo` height on mobile.
- **Un‑stick or compact the topbar.** Make it a slim ~56px bar: logo left, a single
  **"key stat" pill** (e.g. SoC + price) center/right, and a small clock. Keep it
  sticky only if it stays ~56px.
- Move the full status set (action, SoC, price, **Today**, **Month**) into a
  **horizontally‑scrollable pill strip** directly beneath the header (swipeable chips),
  instead of a wrapping vertical stack.

### 2. Navigation → bottom tab bar
- Add an **iOS‑style bottom nav** (fixed, safe‑area padded) for the primary ESS tabs,
  using icon + short label and 44px targets. Implemented set: **Menu · Flow ·
  Schedule · Trends · Advisor** (Battery, Venus, Victron Schedule, Configuration, and
  Replan fold into Menu).
- The app‑views **ESS / Battery / Live(external)** become a compact **segmented control**
  in the header (or the top of "More"), since they're a different axis than the ESS tabs.
- The existing top `.tabs` row is hidden on mobile (`display:none` inside the media
  query); the bottom nav drives the same `data-tab` switching JS (no desktop change).

### 3. Schedule → stacked hour cards
- On mobile, re‑flow each `.hour-row` from an 8‑column grid into a **card**: top line =
  hour + dominant‑action chip + net; second line = the timeline bar full‑width; a
  collapsed detail (price/grid/SoC) revealed on tap. The 15‑min drill‑down becomes a
  nested list. Pure CSS re‑layout of the existing DOM (grid → block/flex in the media
  query) plus the toggle JS we already have.
- Column headers (`.table-head`) hide on mobile (cards are self‑labelling).

### 4. Configuration → stacked, collapsible
- Re‑flow each setting to **two lines** (label + current value as a big tappable target;
  description collapsed behind an "ⓘ" toggle or shown smaller beneath). Keep the
  allow‑listed inline editing.

### 5. Day cost summary → compact card
- Replace the wide justified rows with a tidy **label : value** card (today / actual so
  far / forecast rest / total), right‑aligned values, profit/loss colour.

### 6. Charts → taller, full‑bleed
- Give the SoC/price, monthly‑net, and forecast‑accuracy charts a **taller mobile
  aspect** (e.g. min‑height bump) and full card width; ensure axis labels stay legible.
- Gauges (self‑sufficiency / self‑consumed) stack or shrink gracefully.

### 7. Power‑flow → mobile arrangement
- Either bump the node sizes/label font via a mobile viewBox, or adopt a **vertical
  flow** layout on phones (sources top, house/battery below) so labels stay readable.
  This pairs naturally with the planned **power‑flow v2** (HASS‑inspired) — design the
  v2 to be responsive from the start.

### 8. System polish
- `meta name=viewport` is already present; add `viewport-fit=cover` + safe‑area insets.
- 44px minimum touch targets across nav/tabs/slots/config.
- Respect `prefers-reduced-motion` for the flow dots.

## Implementation strategy used

1. Create `static/css/app.mobile.css`, loaded after `app.css`; everything inside
   `@media (max-width: 680px)`. (Optionally a `680–1024px` tablet tier later.)
2. Build in this order, screenshotting desktop unchanged + mobile improved at each step:
   header → bottom nav → schedule cards → config → day summary → charts → power‑flow.
3. Any JS (bottom‑nav wiring, card toggles) guarded so desktop is untouched.
4. **Desktop regression gate** after each step: compare ≥1024px render to the baseline
   screenshots; must be identical. Then check the 390px render.
5. Test at 390×844 (iPhone 12 Pro) and a couple of other phone widths (360, 414) plus
   landscape.

## Out of scope for this round
- No desktop visual changes whatsoever.
- Tablet (680–1024px) tuning is a later, separate tier.
