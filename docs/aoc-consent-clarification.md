# AOC community sign-up consent

On October 27, 2026, AOC's owner explicitly confirmed that every individual on AOC's list consented to community text messages through a checkbox on newsletter/event sign-up or an in-person event sign-up. Those sources authorize Armenians of Colorado community communications, not only a single event's messages.

The earlier audit's REL-09 conclusion was unsupported. Its synthetic `sms_consent:false` payload demonstrated how an invented field value passed through code; it did not establish that any AOC subscriber lacked consent, that a real sign-up lacked a checkbox, or that event consent was limited to that event. Missing per-person evidence in Twinevia was incorrectly treated as missing consent. That finding is retracted for AOC, and the restrictive local behavior introduced from that assumption is corrected before release.

`COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS` records explicitly confirmed organization sign-up contracts. Its default is the configured AOC organization slug (`armenians-of-colorado`). For those organizations, existing newsletter/community contacts and event sign-ups are eligible for the relevant community audience without per-person re-approval or a fabricated historical evidence backfill. Event sends still select their actual event registrations. Other organizations do not inherit AOC's contract by name similarity or tenant identifiers.

Signed booking source fields remain retained as raw metadata. A missing, unknown, or synthetic false booking flag is not interpreted as withdrawal from AOC's broad community sign-up consent. New signed AOC bookings record the actual organization-level consent basis rather than falsely claiming an individual raw field was true.

Explicit unsubscribe/STOP and administrative opt-out records always block new sends. A recipient's explicit START can restore the existing AOC sign-up consent after removing the transport opt-out; AOC subscribers are not required to re-prove a grant they already gave. A genuine recorded owner withdrawal or corrected old phone address is retained, and ordinary suppression/delivery safety still applies. Event cancellation does not withdraw community communications consent.

The correction changes local code and documentation; it does not delete contacts, alter historical consent records, clear opt-outs, send messages, or modify the production list as a verification action. The production release applies the corrected sign-up contract while preserving existing opt-outs and customer records.
