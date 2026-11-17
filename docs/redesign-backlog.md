# The shell redesign: eight decisions ready to file

The parts of the redesign are already specified — tokens and themes in
[DESIGN.md](DESIGN.md), the conversation in `design-ui-1..3`, and the panels in #182–#213.
This page records the *shell* decisions: where things live when they are not the message.
Each section below is one issue. The heading is the title; **Why**, **Proposal** and
**Acceptance** are the body.

**Status.** The minimal shell (#262) has since made four of these decisions, in simpler
forms than proposed: the composer is one chip plus Send, the sidebar footer is a single
**Settings** row, a healthy connection says nothing, and the welcome screen is one line
above the composer. Sections 1–4 are kept for the reasoning behind them; their **Why**
describes the shell before #262. Decisions 5–8 — turn metadata, the actions gutter, code
and table frames, and the sidebar rail — are still open.

They deliberately do not repeat the existing issues: #184 (one Settings page), #187
(reasoning), #188 (message actions), #189 (markdown), #191 (activity), #194 (new chat
home), #195 (chat list), #197 (composer), #199 (states), #201 (a11y), #202 (phone),
#204 (status bar), #205 (chat view) and #213 (per-chat colour) already own their
territory. These are the layout decisions those issues will be implemented inside.

## 1. The composer becomes a sentence, not a control panel

**Why.** The composer row currently carries five controls before the send button: the
Work/Chat segmented control, the permission picker, the working-folder field, Web on, and
the model picker. On a phone they wrap into two rows; on a desktop they read as a settings
panel attached to a text field, and the same controls are the loudest thing on the welcome
screen.

**Proposal.** Collapse them into one **Context** chip that states the effective setup in
plain words — `Work · Ask first · Web on · qwen3.5:4b` — and opens a popover with the same
controls in labelled groups. The row keeps attach, slash, Context and send. The chip shows
a single dot when anything differs from the default, so the common case is three quiet
controls and a sentence.

**Acceptance.** At 360px the composer is one row. Every control stays reachable by
keyboard and keeps its current label. The chip's summary is generated from state, never
hard-coded.

## 2. Put the sidebar footer behind one Tools entry

**Why.** The sidebar footer lists eight rows — Memory, Reminders, Add provider, Extensions,
Privacy, MCP servers, Install & notifications, Connect your phone — plus the "On your
computer" note and three icon buttons. It is a settings menu wearing a navigation bar's
clothes, and it grows every time a feature lands. The autostart panel is a raw
`<details>` with inline fixed positioning, sitting at the bottom-left of every screen.

**Proposal.** Keep the three rows people use while chatting (Memory, Reminders, Add
provider), move the rest into a **Tools** popover, and let one **Settings** entry open the
single settings page from #184 — with appearance (theme, density, motion) as its first
section. Autostart moves into Settings with the rest of the install options.

**Acceptance.** The sidebar footer is at most four rows plus the status line. No inline
`style` attributes remain in `index.html`. Nothing becomes unreachable: every row that is
removed appears in Tools or Settings.

## 3. One status line, in one place

**Why.** State is announced in five places: the connection chip in the top bar, "On your
computer" in the sidebar, the "Local only" chip, the incognito banner, and the error
banner. A reader has to scan all of them to answer one question: is Kova working, and is it
working the way I asked?

**Proposal.** One status line in the sidebar footer (`Connected · Local only · Work`), with
a popover for detail (provider, model, context use, phone). The top bar keeps the chat
title and its actions. Banners appear only for things that need a decision, not for
states.

**Acceptance.** No state appears twice. The status line is a single element with one
`role="status"`, so a screen reader announces a change once.

## 4. The empty state is an invitation, not a landing page

**Why.** The welcome screen is a headline, a subline, three large starter cards and a
privacy note, centred in a column with a few hundred pixels of empty space under it. The
three cards restate what the composer can already start, and on a phone they push the
composer almost off screen.

**Proposal.** One line of greeting, the composer, and three compact starter chips directly
above it. The note under the composer ("All yours, right here on your machine") plus the
status line already say what the big privacy line says, so it goes.

**Acceptance.** The composer is visible without scrolling at 360×740 and at 1440×900. The
starters are one row on desktop and scroll horizontally on a phone.

## 5. An assistant turn has no avatar and no name

**Why.** Every assistant message repeats a mark, the name "Kova", and a `Thought for 0.0s`
row above the text — roughly 48px of chrome per turn for information that never changes in
a one-assistant conversation. In a long chat it is the loudest repeated element on the
page.

**Proposal.** One quiet metadata line at the top of the turn — time, model, and a
"Thought for 2s" chip that expands — and no avatar. The identity is already in the window
title and the sidebar.

**Acceptance.** A turn's static chrome is at most one 20px line. Reasoning still expands
in place and keeps its `aria-expanded` state.

## 6. Actions live in a fixed gutter, never over the text

**Why.** Message actions (copy, retry, edit, branch, details) have no home yet. The obvious
place — a row under the message — makes the layout jump every time the pointer moves.

**Proposal.** A fixed 40px gutter on the assistant's side, right-aligned to the measure,
holding the actions at low contrast until the turn is hovered or focused. The text never
moves; the gutter is always reserved.

**Acceptance.** Hovering a turn changes no measurement. Actions are reachable by Tab in
reading order, and every icon has a name.

## 7. Code blocks and tables lose their frame

**Why.** A code block currently draws a border, a header bar with a language label and a
copy button, inside a transcript that already has a reading measure. That is two frames
around one thing; tables repeat it at every row.

**Proposal.** A hairline rule above the block, a muted language label on the left and copy
on hover or focus. Tables lose the outer border, keep a sticky header when they are taller
than the viewport, and use the same hairline rules.

**Acceptance.** Nothing in a code block or a table is drawn in an accent colour. Long code
scrolls horizontally without widening the page. Copy works by keyboard.

## 8. A rail, not a switch, for the sidebar

**Why.** The sidebar is either open at roughly 250px or gone entirely, and the control that
changes it lives in the far top-left of the content area, away from the edge it changes. On
a 1440 screen the open sidebar is mostly empty below three conversations.

**Proposal.** A 56px icon rail by default (new conversation, search, recent chats as
initial-marked pills), expanding on hover and pinning on click; the toggle lives in the
rail header. The expanded state is remembered per window, like the desktop app remembers
its bounds.

**Acceptance.** Chat content never shifts when the rail expands over it. Keyboard users can
pin and unpin it. The rail carries no accent unless something is happening.

---

**Filing these.** One issue per open section, title as written, body from the three
paragraphs, labels `design`, `area:ui` and `idea`. That means sections 5–8; the first four
are built. Of the open ones, 5 and 7 are the ones a reader feels first.
