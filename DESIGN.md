---
name: "Twinevia Public Marketing"
description: "The Dispatch Ledger: managed messaging presented as a precise, accountable public record."
colors:
  paper: "#fefefe"
  paper-cool: "#f5f8fc"
  navy-field: "#001839"
  ledger-ink: "#06122e"
  ink-muted: "#44536d"
  action-cobalt: "#1550ee"
  action-cobalt-deep: "#0d3bc0"
  route-cobalt: "#1547d3"
  annotation-vermilion: "#bf2e1b"
  rule: "#d9e3f0"
  rule-strong: "#afbdd1"
  status-success: "#087452"
  status-danger: "#a32819"
  focus-gold: "#ffca43"
  white: "#ffffff"
typography:
  campaign-display:
    fontFamily: '"Oswald", "Arial Narrow", sans-serif'
    fontSize: "clamp(3.6rem, 4.45vw, 5rem)"
    fontWeight: 650
    lineHeight: 1.18
    letterSpacing: "-0.035em"
  display:
    fontFamily: '"Oswald", "Arial Narrow", sans-serif'
    fontSize: "clamp(3.4rem, 7.2vw, 6rem)"
    fontWeight: 700
    lineHeight: 0.92
    letterSpacing: "-0.035em"
  headline:
    fontFamily: '"Oswald", "Arial Narrow", sans-serif'
    fontSize: "clamp(2.5rem, 5vw, 5rem)"
    fontWeight: 700
    lineHeight: 0.95
    letterSpacing: "-0.035em"
  title:
    fontFamily: '"Oswald", "Arial Narrow", sans-serif'
    fontSize: "clamp(1.6rem, 2.5vw, 2.35rem)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.025em"
  body:
    fontFamily: 'ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: 'ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    fontSize: "0.68rem"
    fontWeight: 800
    lineHeight: 1.4
    letterSpacing: "0.09em"
rounded:
  square: "0px"
  field: "3px"
  control: "4px"
  circle: "50%"
spacing:
  compact: "0.5rem"
  control: "0.75rem"
  inline: "1rem"
  gutter: "1.25rem"
  cluster: "2rem"
  content-gutter: "clamp(1.25rem, 4vw, 4rem)"
  feature-gutter: "clamp(1.25rem, 6vw, 7rem)"
  section-block: "clamp(4rem, 8vw, 8rem)"
components:
  button-primary:
    backgroundColor: "{colors.action-cobalt}"
    textColor: "{colors.white}"
    rounded: "{rounded.control}"
    padding: "0.72rem 1.05rem"
  button-primary-hover:
    backgroundColor: "{colors.action-cobalt-deep}"
    textColor: "{colors.white}"
    rounded: "{rounded.control}"
  button-light:
    backgroundColor: "{colors.white}"
    textColor: "{colors.navy-field}"
    rounded: "{rounded.control}"
    padding: "0.72rem 1.05rem"
  field:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ledger-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.field}"
    padding: "0.75rem 0.85rem"
  nav-bar:
    backgroundColor: "{colors.navy-field}"
    textColor: "{colors.white}"
    height: "72px"
    padding: "0.75rem clamp(1.25rem, 3vw, 3.5rem)"
  ledger-surface:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ledger-ink}"
    rounded: "{rounded.square}"
  status-delivered:
    textColor: "{colors.status-success}"
  status-reply:
    textColor: "{colors.route-cobalt}"
  status-failed:
    textColor: "{colors.status-danger}"
---

# Design System: Twinevia Public Marketing

## Overview

**Creative North Star: "The Dispatch Ledger"**

The public marketing world treats managed SMS as an accountable operating record rather than generic SaaS lifestyle theater. Cool-white dockets sit inside a deep navy field; cobalt shows action and routing; one restrained vermilion annotation carries the reply or exception that deserves attention. Condensed Oswald gives campaign-scale urgency while the body sans keeps dense ledgers, policies, forms, and operational copy readable.

The system is precise, near-flat, and editorial. Fine rules, numbered handoffs, recipient-level rows, compact metadata, tabular figures, and mostly square planes make the service feel auditable without drifting into vintage office nostalgia. "Reply in the Margin" is the chosen home-page expression, not a universal page template: the other public routes reuse the visual grammar through split heroes, workflow lines, pricing sheets, policy indexes, forms, and bordered records.

This document governs the public marketing templates and `marketing.css` only. The authenticated workspace, platform administration, setup, billing, and provider-management interfaces retain their own application UI and are not prescribed or redesigned here.

**Key Characteristics:**

- Navy and cool-white ledger planes with cobalt routes and actions.
- Restrained vermilion annotation for a reply connection, caveat, or narrow exception.
- Condensed Oswald display type paired with a readable body sans.
- Fine rules, near-flat depth, square or 4px controls, and circular route nodes.
- Operational geometry built from stages, recipient rows, dockets, and accountable handoffs.

## Colors

The palette is cool, high-contrast, and operational: navy establishes authority, paper planes hold the record, cobalt carries action and route state, and vermilion appears only as an annotation.

### Primary

- **Action Cobalt** (`colors.action-cobalt`): Primary pilot actions and the large blue call-to-action fields.
- **Deep Action Cobalt** (`colors.action-cobalt-deep`): The primary-button hover state and linked policy copy that needs stronger contrast.

### Secondary

- **Route Cobalt** (`colors.route-cobalt`): Workflow lines, route nodes, icons, replied state, scrollbars, and operational emphasis inside a record.

### Tertiary

- **Ledger Vermilion** (`colors.annotation-vermilion`): The home reply connector and source marker, docket number, and provider-approval caveat.
- **Status Success** (`colors.status-success`): Delivered rows and the submission-confirmation mark.
- **Status Danger** (`colors.status-danger`): Failed recipient outcomes.
- **Focus Gold** (`colors.focus-gold`): The visible global keyboard focus outline and active-navigation underline.

### Neutral

- **Navy Field** (`colors.navy-field`): Header, footer, dark workflow sections, the home promise rail, and intake-aside planes.
- **Ledger Ink** (`colors.ledger-ink`): Primary text, headings, data, and strong rules.
- **Muted Ink** (`colors.ink-muted`): Supporting copy, metadata, labels, and explanatory records.
- **Paper** (`colors.paper`): Primary body and docket surface.
- **Cool Paper** (`colors.paper-cool`): Tonal footers, setup panels, alternate sections, notices, and low-contrast row support.
- **Rule / Strong Rule** (`colors.rule`, `colors.rule-strong`): One-pixel separators that organize records before depth is introduced.
- **White** (`colors.white`): Text and controls on navy or cobalt fields.

### Named Rules

**The Cobalt Route Rule.** Use cobalt for movement, action, and selected operational state; it is not a decorative wash.

**The Vermilion Margin Rule.** Vermilion marks one narrow human reply, caveat, or exception at a time; never promote it into a competing primary action color.

**The Cool-White Plane Rule.** Separate public records with paper tone and fine rules before adding another container or effect.

## Typography

**Display Font:** Oswald (self-hosted variable face, with Arial Narrow and sans-serif fallback)

**Body Font:** The native UI sans stack

**Label Font:** The body sans, weighted and tracked for compact operational metadata

**Character:** The pairing joins direct, condensed campaign language to calm operational legibility. Oswald is reserved for claims, major section headings, prices, counts, and record titles; data rows, policy copy, forms, navigation, and metadata stay in the body sans.

### Hierarchy

- **Campaign Display** (`typography.campaign-display`): The home promise and reply payoff only. The promise uses a 66% horizontal optical scale from 961px upward so every fixed campaign line retains a deliberate gutter beside the docket; below 961px it uses the roomier 77% scale. Its mobile size switches to `clamp(3.15rem, 15vw, 4.15rem)` for the promise and `clamp(3.2rem, 16vw, 4.3rem)` for the reply.
- **Display** (`typography.display`): Public page heroes, policy titles, and submission confirmation.
- **Headline** (`typography.headline`): Major dark-band and call-to-action section headings.
- **Title** (`typography.title`): Capability, contact, security, and record headings.
- **Body** (`typography.body`): Default reading copy, form content, policy prose, and record explanations; policy text stays within roughly 72 characters and hero copy within roughly 62 characters.
- **Label** (`typography.label`): Uppercase workflow labels, docket metadata, table headers, and compact section identifiers.

### Named Rules

**The Condense-the-Claim Rule.** Use Oswald to make the claim or number unmistakable; never use it for long policy prose, form help, or dense recipient data.

**The Metadata Stays Sans Rule.** Uppercase labels earn hierarchy through weight and tracking, not a second decorative typeface.

## Layout

The public canvas tops out at 1500px, while long-form and record-heavy grids use 1280px or 1320px maxima. Horizontal gutters scale from 1.25rem to 4rem on content ledgers and to 7rem on major feature bands. Section rhythm is deliberately generous, commonly using `clamp(4rem, 8vw, 8rem)` or a nearby route-specific variant, while the records themselves stay compact.

The reusable public layouts are split page heroes, linear workflow records, bordered two- or three-column ledgers, form-plus-aside intake, and sticky policy index plus reading column. The home page alone uses the "Reply in the Margin" three-zone first surface: promise rail, dispatch docket, and connected reply margin. Its desktop grid is `minmax(280px, 1fr) minmax(560px, 2fr) minmax(270px, 1fr)`; this is signature composition evidence, not a template for other routes. The lower home handoff record uses a two-by-two grid so automated survey flows remain a first-class capability rather than being buried inside inbox copy.

Responsive behavior uses three implemented breakpoints. At 1180px the home first surface becomes two columns, moves the reply margin below them, removes the desktop-only connector, and reflows the offer docket into a two-column ledger with a full-width caveat. At 960px desktop navigation becomes a details menu, the home surface becomes one column, and the major split grids, pricing sheet, security records, contact routes, and intake layout collapse. At 640px the header shortens from 72px to 68px, page gutters settle at 1.25rem, policy navigation becomes a two-column index, forms become one column, and the compact four-column recipient table fits the docket without an internal horizontal scroller.

### Named Rules

**The Composition-Is-Not-a-Template Rule.** Carry the palette, typography, rules, spacing, and operational geometry across public pages; keep the three-zone reply composition exclusive to the home first surface.

**The Record Must Stay Legible Rule.** Reflow the surrounding layout before shrinking stages, labels, or recipient rows below their usable density.

## Elevation & Depth

The system is flat by default. Paper tone, navy fields, one-pixel rules, and inset annotation strokes establish hierarchy; ordinary capability, pricing, security, contact, policy, and form surfaces do not float. The active dispatch docket alone receives the low structural `docket-low` shadow, and the opened mobile navigation uses the stronger `menu-overlay` shadow because it is a true overlay. Route dots and reply markers use one-pixel rings to stay crisp rather than soft glow.

### Shadow Vocabulary

- **Docket Low** (`shadows.docket-low`): A restrained shadow beneath the home dispatch record, used to separate the active paper docket from the navy field.
- **Menu Overlay** (`shadows.menu-overlay`): The only pronounced overlay shadow, reserved for the open mobile navigation panel.

### Named Rules

**The Flat-Ledger Rule.** Use tone and rules at rest; add shadow only when a surface is materially active or overlays another plane.

## Shapes

Public surfaces are square (`rounded.square`). Inputs use a barely softened 3px corner (`rounded.field`), while buttons stop at 4px (`rounded.control`). Circular geometry is functional: route nodes, status dots, offer icons, reply origins, and the dispatch docket's repeating left-edge perforations. Fine separators are normally 1px; route paths and the vermilion reply annotation use 2px strokes.

### Named Rules

**The Square-Docket Rule.** Do not turn public records into a rounded-card system; curvature belongs to controls and meaningful route markers.

## Components

### Buttons

- **Shape:** Compact controls with a 4px corner and a 44px minimum height; large actions use a 50px minimum height.
- **Primary:** Action cobalt with white text, 0.72rem by 1.05rem padding, and heavy body-sans labeling; large actions increase inline padding to 1.3rem.
- **Hover / Active:** Hover changes to deep cobalt and lifts 2px; active returns to the baseline. Color transitions run for 160ms and the lift uses 240ms with `cubic-bezier(0.16, 1, 0.3, 1)`.
- **Light:** White on cobalt sections with navy text; hover moves to the implemented pale-blue fill (`#e8effd`).
- **Focus:** The global 3px focus-gold outline sits 3px outside the control.

### Text Links

Text links are heavy, underlined actions with a 0.35em underline offset and a compact 0.4rem icon gap. Light variants remain white on navy or cobalt. They do not imitate buttons.

### Inputs / Fields

- **Style:** White fields, ledger-ink text, a 1px strong-rule border, a 3px corner, 0.75rem by 0.85rem padding, and a 48px minimum height. Textareas start at 170px and resize vertically.
- **Hover:** The border strengthens to the implemented blue-gray (`#7e91ac`).
- **Focus:** The border becomes action cobalt and gains a 3px pale-cobalt outline (`#c6d4ff`) with a 1px offset.
- **Help:** Labels use 0.78rem heavy body sans; supporting help uses 0.69rem muted ink.

### Navigation

The public header is a 72px navy grid with a white wordmark, cobalt paper-plane mark, centered desktop links, quiet sign-in action, and one primary pilot button. The active route is white with a 2px focus-gold underline offset by 0.5rem. At 960px it becomes a native `details` disclosure whose summary preserves a 44px target and opens a square navy overlay. The footer repeats the navy plane with three link/identity columns, a ruled action area, and restrained cool text.

### Status Labels

Recipient status is an inline heavy label preceded by a 7px circular dot. Delivered is green, replied is route cobalt, and failed is red. The replied row also receives a cool tint and a 2px vermilion inset rule that connects to the reply annotation; conversation content begins 0.65rem beyond that terminal rule so the annotation never crosses the message icon. Status is never communicated by color alone because each state remains written.

### Cards / Containers

There is no generic floating-card primitive. Capability, pricing, security, contact, policy, and company records are square paper or cool-paper planes organized by 1px rules and grid alignment. Internal padding scales by context, most often from 1.25rem to 4rem. Use the active dispatch docket as the only low-lift paper container and the mobile menu as the only strong overlay.

### Offer Metric Lockup

Each home-page commercial item is one compact lockup: a circular route icon occupies the first column while the price and its descriptor share one aligned text column. Both lines use Oswald within its declared weight range; the descriptor is no smaller than 0.92rem, carries a 650 weight, and sits within 0.18rem of the value instead of dropping below the icon. Provider approval is intentionally subordinate: it appears as one small asterisk footnote beneath the four-item ledger, without a warning icon, metric title, or competing tile. At 1180px and below the commercial ledger becomes two columns; the text-to-icon relationship must remain intact at the 320px floor.

### Dispatch Docket and Reply Annotation

The home dispatch docket is the signature operational proof: a square paper record with a perforated left edge, condensed title, compact metadata, three cobalt route nodes, a recipient-attempt table, and a cool-paper audit footer. On wide screens, the adjacent reply margin uses an oversized Oswald quote, compact conversation rows, and one response-anchored vermilion L path that points back into the docket without independent endpoints that can drift. The path terminates on the response-row rule, while a fixed content inset keeps the chat icon visibly clear of the stroke. When the panels stack, the connector is omitted; the written replied state, reply copy, and metadata preserve the relationship. Do not reduce this pattern to decorative chat bubbles.

### Notices

Flash notices are square, ruled, and text-led. The neutral notice uses cool paper; error and success notices use pale tonal backgrounds with darker text and border colors. Submission success uses a square green 52px mark rather than a rounded badge.

### Motion

Motion explains the home route: the line draws over 720ms, route nodes arrive over 320ms with 180ms and 360ms delays, and the reply connection draws over 520ms after a 620ms delay. Buttons use only the small hover lift described above. Under `prefers-reduced-motion: reduce`, animations and transitions collapse to 0.01ms, run once, and smooth scrolling is disabled.

### Named Rules

**The State-Must-Explain Rule.** Pair every color state, route node, and annotation with readable text or metadata that explains what happened.

## Do's and Don'ts

### Do:

- **Do** keep cobalt reserved for primary actions, route lines, and selected operational states.
- **Do** use fine 1px cool rules and cool-white tonal planes to separate dense records before adding elevation.
- **Do** keep long public copy in the body sans with readable measure, and preserve all four recipient-table columns on narrow screens through the compact fixed layout.
- **Do** preserve yellow 3px focus outlines and the reduced-motion override on public interactions and route animations.
- **Do** treat "Reply in the Margin" as the home page's signature expression while carrying only its palette, typography, rule, shape, and data-geometry language to other public pages.

### Don't:

- **Don't** apply the three-zone home hero to every marketing route; split heroes, policy indexes, forms, and ledgers already provide the broader public system.
- **Don't** extend this document to authenticated workspace, platform-admin, setup, billing, or provider UI.
- **Don't** introduce beige paper, vintage office forms, typewriter nostalgia, glass surfaces, generic bento cards, or a rounded-card system. The only gradient technique in the built CSS is the monochrome radial repeat that cuts the docket perforations.
- **Don't** use vermilion as a broad fill or second primary action color; it marks the reply connection, a caveat, or a narrow exception.
- **Don't** add testimonials, customer logos, awards, certifications, guarantees, or decorative analytics the product cannot substantiate.
