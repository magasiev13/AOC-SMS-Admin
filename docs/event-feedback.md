# Event feedback

Open an event, including an event in **Past events**, and choose **Event feedback**.
Create a short form with a unique reply keyword and one question per line, then
choose **Review invitation**. The composer selects that event and suggests the
invitation text. Opening or saving a feedback form does not send a message;
the normal audience, timing, eligibility and signed confirmation review still
controls dispatch. Actual opt-outs and suppressions remain effective.

People reply with the keyword and answer one question at a time. Responses and
completed/started totals appear on the feedback page, with links to the existing
inbox conversation. Staff see names or respondent record labels, with phone-like
content redacted. This workflow provides no recipient dataset export.

Feedback is an explicit `event_feedback` survey purpose, associated with an event
through `feedback_event_id`. It does not use registration `linked_event_id`.
Answers are stored as survey responses, not attendee names, contact names,
registrations, booking spaces, party sizes or WordPress bookings. No feedback
completion calls a registration transition, even if a legacy/manual writer
incorrectly adds a registration link to the flow.

Each new feedback session freezes its purpose, event ID/title, question prompts
and thank-you message. Editing a form affects future starts; an existing
conversation continues with its original context. Saved responses retain each
original question prompt. Invalid or missing frozen prompts cancel the session
without inventing completion or changing attendance.

**Pause feedback** stops new keyword starts and retains all responses. Existing
conversations may continue with their original prompts. STOP remains authoritative
and cancels future unsubmitted replies. Durable automatic reply keys, provider
acceptance evidence, retry/expiry handling and deleted-session guards are shared
with the existing survey/outbox engine. An uncertain provider outcome is not
permission to send again.

The original general-survey workflow remains compatible. Its optional event
registration link still treats the first answer as the attendee name. Use the
explicit event feedback workflow for ratings and comments; old survey/session
history is not silently reinterpreted.

SaaS migration028 adds feedback context columns with `survey` as the purpose for
all existing rows, and null feedback context. Legacy SQLite migration027 supplies
the same additive columns for compatibility. Existing names, registration rows,
questions, answers, counters and provider identities are not backfilled or
rewritten. Both migrations are idempotent. After feedback is available, recovery
must retain its purpose guard and frozen context; selecting older code that can
turn an answer into a registration is not a safe standalone rollback.
