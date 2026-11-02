"""Task-focused UI guidance. Static content never includes contact or provider data."""
from __future__ import annotations


WORKSPACE_TOPICS = (
    {'id': 'getting-started', 'title': 'Get Started',
     'summary': 'Learn the three tasks most teams use first.',
     'keywords': 'new beginner start first overview dashboard workspace',
     'steps': ('Open Contacts to see your saved people. An owner or admin can add or import contacts.',
               'Open Send a Message, choose your audience, and review the recipients and message before confirming.',
               'Open Inbox to read incoming messages and reply to conversations.'),
     'actions': (('Open Contacts', 'main.community_list'), ('Send a Message', 'main.dashboard'), ('Open Inbox', 'main.inbox_list'))},
    {'id': 'messaging-setup', 'title': 'Finish Messaging Setup',
     'summary': 'Understand what you handle and what support handles.',
     'keywords': 'setup approval carrier a2p twilio provider billing subscription plan sender number waiting activate',
     'steps': ('Your workspace is your organization’s private area. You can explore it while live messaging is being prepared.',
               'The owner reviews billing and completes Organization Details in five parts: organization, contact details, authorized representative, signup and permission, and messaging. Back and Continue keep your entries; Save Organization Details saves all five parts together.',
               'Twinevia support handles the sending connection and number.',
               'Messaging Setup shows the next required task. Live sending stays paused until both billing and the messaging connection are ready.'),
     'actions': (), 'owner_actions': (('Open Messaging Setup', 'main.setup'), ('Review Billing', 'main.billing_overview'))},
    {'id': 'send-messages', 'title': 'Send or Schedule a Message',
     'summary': 'Choose who receives a message and when it goes out.',
     'keywords': 'send compose sms text broadcast blast audience schedule scheduling later timezone review test segments units',
     'steps': ('Choose Contact List for eligible contacts with list permission, or Event Registrants for a selected event.',
               'Write the message. Open delivery options to schedule it or use an approved internal test list.',
               'Review the audience, timing and estimated SMS usage. Only the final confirmation sends or schedules the message.'),
     'actions': (('Send a Message', 'main.dashboard'), ('Scheduled Messages', 'main.scheduled_list'))},
    {'id': 'contacts', 'title': 'Understand Contacts and Opt-Outs',
     'summary': 'A saved contact and permission to send are separate.',
     'keywords': 'contact community people directory phone import csv permission consent stop opt out suppression block',
     'steps': ('Contacts is your saved directory. The audience preview shows which contacts are currently eligible for the message you are preparing.',
               'Opt-Outs & Blocks lists people who unsubscribed or are excluded from sending. Removing a list entry does not by itself grant SMS permission.',
               'Your workspace role controls contact tools. Ask the owner if phone numbers, imports or exports are unavailable. Staff can work with names and records.'),
     'actions': (('Open Contacts', 'main.community_list'), ('Opt-Outs & Blocks', 'main.unsubscribed_list'))},
    {'id': 'events', 'title': 'Manage an Event',
     'summary': 'Keep bookings, reserved places and message recipients distinct.',
     'keywords': 'event registration booking seats places attendance roster inventory source reconcile sync feedback',
     'steps': ('Overview shows roster verification and tasks that need attention. One group booking may reserve several places but supply one contact. Booked places do not prove attendance.',
               'Open Registrations to review bookings and messaging contacts. Expand Add Booking or Import CSV when you need those tools. Contact pages show up to 100 contacts; event totals and message audiences include the whole event.',
               'Choose Message Registrants to prepare an event message. Open Messages to review scheduled messages, or Feedback to manage a feedback form and request responses. Feedback invitations need a separate audience, timing and confirmation.'),
     'actions': (('Open Events', 'main.events_list'),)},
    {'id': 'automations', 'title': 'Set Up Automatic Replies or Surveys',
     'summary': 'Respond to incoming keywords or collect a sequence of answers.',
     'keywords': 'automation automatic reply keyword trigger rule survey flow form registration question answers feedback',
     'steps': ('Automatic Replies responds to an exact keyword. For example, someone texts INFO and receives the reply you configured.',
               'Surveys asks questions one at a time. A registration survey can add or update an event booking.',
               'Event Feedback collects answers separately from registrations. Creating a form never creates a feedback invitation.'),
     'actions': (('Automatic Replies', 'main.keyword_rules_list'), ('Surveys', 'main.survey_flows_list'))},
    {'id': 'message-history', 'title': 'Check Message Results',
     'summary': 'Follow a message from scheduling to its reported outcome.',
     'keywords': 'history logs delivery delivered accepted failed error unknown unresolved held retry cancel scheduled queue',
     'steps': ('Scheduled Messages shows messages waiting to run, held for review, or already processed. Open a message to inspect its current state.',
               'Message History shows submission and delivery evidence. Provider acceptance means the delivery service accepted the message; it does not confirm delivery.',
               'For a held or unresolved send, use the action and explanation shown on that record. Do not create a replacement just to retry an uncertain outcome.'),
     'actions': (('Scheduled Messages', 'main.scheduled_list'), ('Message History', 'main.logs_list'))},
    {'id': 'settings', 'title': 'Manage Workspace Settings',
     'summary': 'Find team access, billing, tests and optional app connections.',
     'keywords': 'settings team users role owner staff security integration app webhook wordpress connect secret test recipients',
     'steps': ('Messaging Setup explains activation. Billing shows your plan and payment status; these are separate from messaging approval.',
               'Team Access manages who can use this workspace. Test Recipients is the owner-approved list for internal test messages.',
               'App Integrations is optional: it connects an external registration or event system. A developer configures its connection details.'),
     'actions': (), 'admin_actions': (('Open Settings', 'main.settings_home'),), 'admin_only': True},
)

PLATFORM_TOPICS = (
    {'id': 'platform-start', 'title': 'Manage Customer Workspaces',
     'summary': 'Each organization has a separate workspace and team.',
     'keywords': 'start organizations customer company community client workspace onboarding invite owner',
     'steps': ('Open Organizations to find the customer you are helping.',
               'Review that organization’s owner invitation, billing and messaging status separately.',
               'Use an owner or staff account for workspace activity. Platform access does not act as a customer’s account.'),
     'actions': (('Open Organizations', 'main.platform_organizations_list'),)},
    {'id': 'platform-activation', 'title': 'Understand Messaging Activation',
     'summary': 'Separate customer tasks from support tasks.',
     'keywords': 'setup activate a2p twilio carrier provider registration sender number billing',
     'steps': ('The owner supplies required organization details and handles billing.',
               'Platform support reviews the connection, carrier registration and sending number for that organization.',
               'Use the organization’s current next action and error details. Do not mark billing or messaging ready to bypass an incomplete requirement.'),
     'actions': (('Review Organizations', 'main.platform_organizations_list'),)},
    {'id': 'platform-access', 'title': 'Review Access and Requests',
     'summary': 'Keep platform administration separate from workspace roles.',
     'keywords': 'pilot request user admin owner staff security access role',
     'steps': ('Pilot Requests contains incoming requests for a workspace.',
               'Users manages sign-in accounts; organization membership determines workspace access.',
               'Security records account activity. Check the affected organization and account before changing access.'),
     'actions': (('Pilot Requests', 'main.platform_pilot_applications'), ('Users', 'main.users_list'), ('Security', 'main.security_events'))},
)


def help_topics(*, platform=False, admin=False, owner=False, saas=True, query=''):
    """Return role-appropriate guidance; URLs are resolved by the request layer."""
    result = []
    for topic in PLATFORM_TOPICS if platform else WORKSPACE_TOPICS:
        if topic.get('admin_only') and not admin:
            continue
        if query and query.casefold() not in ' '.join((topic['title'], topic['summary'], topic['keywords'], *topic['steps'])).casefold():
            continue
        item = {**topic, 'actions': list(topic['actions'])}
        if owner and saas:
            item['actions'].extend(topic.get('owner_actions', ()))
        if admin and saas:
            item['actions'].extend(topic.get('admin_actions', ()))
        result.append(item)
    return result


PAGE_HELP = (
    (('main.dashboard',), 'Send a Message', 'send-messages',
     'Prepare a text message for eligible contacts, then review it before confirming.',
     ('Choose Contact List or Event Registrants.', 'Write your message and choose now, later or an approved test.', 'Review the recipients, timing and SMS usage before confirming.')),
    (('main.settings_home',), 'Settings', 'settings',
     'Manage the workspace rather than individual messages.',
     ('Use Messaging Setup for activation and Billing for payment status.', 'Use Team Access to manage people who can sign in.', 'Connect an external app only if you need registrations or events synced from another system.')),
    (('main.event_sync_',), 'App Integrations', 'settings',
     'Connect an external event or registration app. This is optional and usually needs a developer.',
     ('Choose Connected Apps or WordPress.', 'Your developer configures the endpoint and signing secret in the source app.', 'After a complete sync, compare and review its event roster. Syncing does not send a feedback invitation.')),
    (('main.community_',), 'Contacts', 'contacts',
     'View your saved directory. A saved record is not automatically an SMS recipient.',
     ('Search or open a contact record.', 'Owners and admins can add or import contacts.', 'Use the message audience preview to check current permission and exclusions.')),
    (('main.unsubscribed_',), 'Opt-Outs & Blocks', 'contacts',
     'Review contacts excluded from messaging and the reason for each exclusion.',
     ('An opt-out records a recipient’s request to stop messages.', 'A block can prevent sending for another recorded reason.', 'Changing this list does not by itself establish new SMS permission.')),
    (('main.events_', 'main.event_', 'event_feedback.'), 'Events and Feedback', 'events',
     'Keep an event’s registrations, communication and optional feedback together.',
     ('Bookings are transactions; reserved places are the people those bookings represent.', 'Reachable contacts are eligible SMS destinations, not a headcount.', 'Feedback collects responses separately and is invited only after an explicit review.')),
    (('main.keyword_rule',), 'Automatic Replies', 'automations',
     'Send a configured reply when someone texts a keyword.',
     ('Choose the exact keyword people will text, such as INFO.', 'Write the automatic reply and activate the rule when it is ready.', 'Use Surveys when you need a sequence of questions instead of one reply.')),
    (('main.survey_flow',), 'Surveys', 'automations',
     'Collect a sequence of answers through text messages.',
     ('Choose a keyword to start the survey.', 'Add the questions and review what happens when it is completed.', 'A registration survey can affect bookings; event feedback does not.')),
    (('main.scheduled_',), 'Scheduled Messages', 'message-history',
     'Inspect messages scheduled for later and any sends that need review.',
     ('Check the audience and scheduled time.', 'Open a held message to see what needs attention.', 'Accepted or uncertain submissions retain their existing history when you review a hold.')),
    (('main.logs_', 'main.log_'), 'Message History', 'message-history',
     'Review what was submitted and what delivery evidence is available.',
     ('Accepted means the delivery service accepted the submission.', 'Delivered requires a delivery report; it is a separate outcome.', 'Read failure or unresolved details before deciding on a next action.')),
    (('main.inbox_',), 'Inbox', 'getting-started',
     'Read incoming messages and reply to individual conversations.',
     ('Choose a conversation to see its messages.', 'Read the recent exchange before preparing a reply.', 'Use Automatic Replies or Surveys only for a reusable incoming-message workflow.')),
)


def page_help(endpoint, *, platform=False):
    if platform or not endpoint:
        return None
    for prefixes, title, topic, summary, steps in PAGE_HELP:
        if any(endpoint == prefix or endpoint.startswith(prefix) for prefix in prefixes):
            return {'title': title, 'topic': topic, 'summary': summary, 'steps': steps}
    return None
