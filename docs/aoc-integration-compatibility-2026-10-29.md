# AOC WordPress compatibility inspection, 2026-10-29

Read-only SSH/WP-CLI inspection established the compatibility boundary below before
implementation. No plugin, option, form, entry, payment or messaging setting was
changed, and no webhook or live test message was submitted.

The standalone repository `twinevia-event-sync.php` plugin is not installed or
loaded on the inspected live WordPress site. There are no `twinevia_*` integration
options. The actual integration lives in the must-use file
`wp-content/mu-plugins/aoc-custom-site-behavior.php`, using
`aoc_custom_twinevia_*` functions, the `aoc-wordpress` source and AOC signature
headers. Its inspected SHA256 was
`0b6129f6a0aae16320009bbc6cb0ebd9f9afa93880c35b06a548ae96d863a027`.
Active relevant plugins were Events Manager 7.4.12 and WPForms 2.0.3.

Representative source bindings:

| Event | Events Manager ID | Event post ID | Distinct embedded WPForms ID | Group-size field |
|---|---:|---:|---:|---|
| Trunk or Treat Party | 59 | 100009684 | 100009682 | Field 4, Total Number of Attendees |
| Community Hike | 58 | 100009631 | 100009626 | Field 4, Number of Hike Attendees |
| Armenian Youth Network Event | 55 | 100009214 | 100009211 | Not inspected |
| Picnic 2026 | 53 | 100009056 | 100009054 | Not inspected |
| Board Game Night | 52 | 100009043 | 100009048 | Not inspected |

The inspected explicit `AOC_TWINEVIA_WPFORMS_EVENT_MAP` accessor returned an empty
map; the installed integration discovers unambiguous embedded event forms. The
wine/brandy form 100009250 has payment-select ticket-quantity fields 18/19,
payment-total 22, Stripe card 23 and text-message permission checkbox 31. The source
helper adds ticket quantities and requires a total between 1 and 10. The bridge
preserves that helper for paid ticket forms and explicitly maps the two inspected
free RSVP group-size fields without assuming every form has the same structure.

The installed Events Manager active statuses are 0 (pending) and 1 (approved).
Its paid WPForms path includes completed/processed live payments only, and the
Stripe completion hook also requires a succeeded Stripe response. The installed
booking payload includes seats, selections and comments, but creates a fresh
`updated_at` at replay time; the updated adapter instead persists content revisions.
The old event-booking CLI replays all existing source rows but sends only a global
`reconcile_complete` heartbeat. It cannot prove absence of a deleted record.

The optional repository bridge is prepared for these actual hooks/settings.
The shared application protocol, booking ledger, inventory proof and workspace
are provider neutral and intended for all Twinevia organizations. AOC is only a
compatibility case. Source authority remains disabled until an operator compares
a complete inventory with that organization's source.

PHP syntax checks and the standalone eleven-assertion WordPress API simulation were
executed through the available remote PHP interpreter with code supplied on stdin.
Those checks did not bootstrap WordPress or invoke source/messaging hooks. They
verify code syntax, immutable form/entry binding, stable source revisions, party-size
handling, bounded pages, exact page-proof computation and late-revision snapshot fences; they do not certify the
new plugin running inside the live WordPress process. Live installation is deferred.
