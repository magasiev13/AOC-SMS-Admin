## PHASE 1 — Critical (visual hierarchy, usability, responsiveness, or consistency issues that actively hurt the experience)

- [Dashboard / Primary workflow]: The message composer is pushed below dense KPI/links blocks and visual noise competes with the send action → Move “Send SMS Blast” to the top of dashboard content and demote non-critical panels below it → Users should understand and execute the main task in under 2 seconds.
- [Mobile list/detail tables: Community, Events detail, Logs detail, Suppression, Users, Keyword/Survey lists]: Critical columns/actions clip or require horizontal scroll, with controls partially off-screen on mobile → Keep desktop tables, but add explicit mobile card-list layouts for all data-heavy pages and hide wide tables below lg → Prevents hidden actions and eliminates interaction failures on touch devices.
- [Header and page actions across screens]: Mobile header plus per-page action clusters create crowded top sections with competing button styles/weights → Introduce a strict action hierarchy: one primary action visible, secondary actions grouped into a compact overflow pattern on mobile → Reduces cognitive load and improves decision speed.
- [Inbox]: Thread list density and message pane contrast/spacing make scanning hard, especially with long thread lists → Increase thread row spacing, strengthen selected state, and normalize metadata contrast/size; keep composer visually anchored → Improves readability and lowers message handling effort.
- [Touch targets and small interactions]: Multiple clickable controls remain under effective touch comfort (breadcrumbs, sort triggers, some action buttons) → Enforce a minimum 44px touch target for touch contexts and replace tiny interaction affordances with larger controls on mobile/tablet → Improves accessibility and reduces mis-taps.
## Review: These are highest priority because they directly affect task completion, discoverability, and touch usability, not just aesthetics.

## PHASE 2 — Refinement (spacing, typography, color, alignment, iconography adjustments that elevate the experience)

- [Global visual language]: Heavy gradients, high-saturation accents, and multiple decorative effects fragment the brand voice → Reduce to a restrained neutral + single-accent system with semantic status colors only → Creates a calmer, more premium, more trustworthy interface.
- [Typography system]: Overuse of uppercase table headers, mixed font emphasis, and dense microtext reduces clarity → Standardize heading/body scale and remove forced uppercase on table headers → Improves legibility and hierarchy consistency.
- [Navigation and branding]: Animated RGB brand effects and pulse states draw attention away from core content → Remove ornamental brand animations and keep branding static/subtle → Keeps visual focus on user tasks.
- [Buttons, badges, and action rows]: Action styles vary by screen and many rows rely on hover reveal patterns that do not translate well → Standardize button priority mapping and keep essential row actions visible by default → Consistent behavior across devices and predictable interaction model.
- [Spacing rhythm]: Card, section, and control spacing varies noticeably across templates → Normalize vertical rhythm using a single spacing ladder and consistent card internals → Improves polish and scanning flow.
## Review: Phase 2 follows Phase 1 because visual restraint and consistency only matter after layout and interaction issues are fixed.

## PHASE 3 — Polish (micro-interactions, transitions, empty states, loading states, error states, dark mode, and subtle details that make it feel premium)

- [Empty states]: Empty states exist but voice, structure, and CTA emphasis are inconsistent between modules → Create one empty-state pattern (icon, title, one-line explanation, one clear CTA) and apply globally → Gives a cohesive, intentional feel in low-data scenarios.
- [Loading states]: Skeleton/loading treatment is present but not consistent in density and visual weight across lists → Standardize skeleton token sizes and apply evenly to all list/table screens → Perceived performance feels stable and predictable.
- [Motion]: Current motion mixes useful and decorative effects → Keep only functional motion (state change, status feedback, panel transition) with unified timing/easing → Interactions feel responsive without distraction.
- [Accessibility finishing]: Focus styles are generally present, but contrast/tap behavior and small controls need harmonization → Normalize focus ring contrast and ensure keyboard/touch parity across all actionable controls → Raises baseline accessibility and QA reliability.
## Review: Phase 3 compounds the earlier work by improving perceived quality and consistency once structure and hierarchy are corrected.

DESIGN_SYSTEM (.md) UPDATES REQUIRED:

Add a formal token set (colors, type scale, spacing, radii, elevation, motion) before implementation, since no DESIGN_SYSTEM.md currently exists.
Add semantic color tokens for surface, surface-muted, text-primary, text-secondary, border-subtle, accent-primary, accent-primary-hover, status-success/warning/error/info; remove multi-gradient dependency as default style.
Add interaction tokens for touch size (44px minimum), focus ring, hover/pressed states, and row action visibility behavior.
Add component specs for Data Card List (mobile), Page Action Bar (mobile overflow), Empty State, and Table Header typography/casing rules.
These must be approved and documented before build execution.
IMPLEMENTATION NOTES FOR BUILD AGENT:

dashboard.html: breadcrumbs currently live-indicator (line 8) → replace with static “Overview” label; remove pulsing status from default dashboard header.
dashboard.html: move section Send SMS Blast (lines 225-351) above stats/charts (before line 15) so primary action appears first.
dashboard.html: remove Quick Links block (lines 130-162) from dashboard body; keep global navigation as the single route switcher.
app.css: .brand-icon (line 157) animation: border-glow... → animation: none; remove glow box-shadow sequence from @keyframes border-glow.
app.css: .brand-text::after (lines 241-262) decorative RGB underline → disable (content: none) for production UI.
app.css: .table thead th (line 1437) text-transform: uppercase → text-transform: none; letter-spacing: 0.025em → 0.
app.css: .row-actions (lines 625-633 and 1694-1702) hover-gated opacity → always visible (opacity: 1) for touch parity.
app.css: stat-card variants (lines 862-884) multi-gradient backgrounds → neutral surface cards with a single accent indicator and semantic status tint only.
app.css: .quick-link:hover (lines 1183-1189) gradient + lift → subtle surface tint + border emphasis without color inversion.
list.html: table is always rendered (line 68) → convert to desktop table + mobile card-list pattern (same responsive structure used in community/events/logs list templates).
list.html: current table-only layout (lines 19-91) → add mobile card-list variant with fully visible edit/delete actions and metadata labels.
detail.html: registration table-only block (lines 76-147) → add mobile stacked registration cards and hide table on small screens.
detail.html: recipient details table-only block → add mobile stacked recipient rows to prevent clipped status/error columns.
keywords_list.html: wide table-only layout (lines 33-83) → add mobile card-list item layout with keyword, status, match count, and full-width actions.
surveys_list.html: wide table-only layout (lines 33-92) → add mobile card-list item layout with trigger, question count, status, and actions.
base.html: keep existing nav routes but add mobile page-action overflow pattern so page_actions never wrap into multi-row button clusters.