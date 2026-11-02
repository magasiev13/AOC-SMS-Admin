"""Role-aware help explains the workspace without widening access or exposing contacts."""
from html.parser import HTMLParser

import pytest

from tests.test_event_feedback import feedback


class HelpDocument(HTMLParser):
    """Inspect navigation regions and links without adding a parsing dependency."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, response):
        super().__init__()
        assert response.status_code == 200
        self.links, self.topics, self.current_navigation, self.stack = [], [], [], []
        self.query = None
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        region = self.stack[-1][1] if self.stack else None
        if attrs.get('aria-label') == 'Primary Navigation':
            region = len(self.current_navigation)
            self.current_navigation.append([])
        if tag == 'a':
            self.links.append(attrs.get('href', ''))
            if (region is not None and attrs.get('aria-current') == 'page'
                    and 'app-nav-link' in attrs.get('class', '').split()):
                self.current_navigation[region].append(attrs['href'])
        if tag == 'details' and 'data-help-topic' in attrs:
            self.topics.append(attrs['id'])
        if tag == 'input' and attrs.get('name') == 'q':
            self.query = attrs.get('value', '')
        if tag not in self.VOID:
            self.stack.append((tag, region))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def action_endpoints(topics):
    return {endpoint for topic in topics for _, endpoint in topic['actions']}


def test_help_actions_follow_role_without_leaking_previous_owner_actions():
    from app.services.help_content_service import help_topics
    owner = help_topics(owner=True, admin=True)
    assert {'main.setup', 'main.billing_overview', 'main.settings_home'} <= action_endpoints(owner)
    staff = help_topics()
    assert not {'main.setup', 'main.billing_overview', 'main.settings_home'} & action_endpoints(staff)
    assert 'settings' not in {topic['id'] for topic in staff}
    assert {'main.community_list', 'main.dashboard', 'main.inbox_list'} <= action_endpoints(staff)
    admin = help_topics(admin=True)
    assert 'main.settings_home' in action_endpoints(admin)
    assert not {'main.setup', 'main.billing_overview'} & action_endpoints(admin)
    legacy = help_topics(owner=True, admin=True, saas=False)
    assert not {'main.setup', 'main.billing_overview', 'main.settings_home'} & action_endpoints(legacy)


def test_platform_guidance_stays_separate_and_search_matches_user_tasks():
    from app.services.help_content_service import help_topics
    platform = help_topics(platform=True, admin=True, owner=True)
    assert {topic['id'] for topic in platform} == {'platform-start', 'platform-activation', 'platform-access'}
    assert 'main.platform_organizations_list' in action_endpoints(platform)
    assert not {'main.dashboard', 'main.community_list', 'main.setup'} & action_endpoints(platform)
    assert 'messaging-setup' in {topic['id'] for topic in help_topics(query='CaRrIeR')}
    assert 'contacts' not in {topic['id'] for topic in help_topics(query='CaRrIeR')}
    assert help_topics(query='no-matching-help-phrase') == []


@pytest.mark.parametrize('endpoint,topic,title', [
    ('main.event_sync_settings', 'settings', 'App Integrations'),
    ('main.event_sync_status', 'settings', 'App Integrations'),
    ('main.log_detail', 'message-history', 'Message History'),
    ('main.event_detail', 'events', 'Events and Feedback'),
    ('event_feedback.invite', 'events', 'Events and Feedback'),
    ('main.keyword_rules_list', 'automations', 'Automatic Replies'),
    ('main.survey_flows_edit', 'automations', 'Surveys'),
])
def test_contextual_guide_maps_distinct_tasks_without_prefix_collisions(endpoint, topic, title):
    from app.services.help_content_service import page_help
    guide = page_help(endpoint)
    assert guide['topic'] == topic and guide['title'] == title
    assert guide['summary'] and guide['steps']
    assert page_help(endpoint, platform=True) is None
    assert page_help('main.unknown_surface') is None
    assert page_help(None) is None


def test_help_route_filters_actions_and_cannot_select_another_tenant(feedback):
    f = feedback
    f.other.name = 'Private Customer Workspace'
    f.db.session.add(f.m.CommunityMember(organization=f.other, name='Private Other Contact', phone='+17205550145'))
    f.db.session.commit()
    owner_response = f.client.get('/help', query_string={'organization_id': f.other.id})
    owner = HelpDocument(owner_response)
    assert {'/settings', '/billing', '/setup'} <= set(owner.links)
    assert owner.current_navigation == [['/help'], ['/help']]
    assert b'You can prepare a message and review its audience before sending.' in owner_response.data
    assert b'built-in method' not in owner_response.data
    response = f.staff_client.get('/help', query_string={'organization_id': f.other.id, 'topic': 'platform-access'})
    staff = HelpDocument(response)
    assert not {'/settings', '/billing', '/setup'} & set(staff.links)
    assert '/dashboard' in staff.links and 'settings' not in staff.topics
    assert b'Team Member' in response.data and b'Messaging Is Ready' in response.data
    for private in [f.other.name, 'Private Other Contact', '+17205550145', f.profile.from_number,
                    f.profile.twilio_subaccount_sid, f.profile.messaging_service_sid]:
        assert private.encode() not in response.data
    assert f.staff_client.get('/settings').status_code == 403
    assert f.staff_client.get('/billing').status_code == 403
    f.profile.provider_status = 'pending'
    f.db.session.commit()
    waiting_owner = f.client.get('/help')
    assert b'Use Messaging Setup for your next task and the work support is handling.' in waiting_owner.data
    assert '/setup?step=compliance' in HelpDocument(waiting_owner).links
    waiting_staff = f.staff_client.get('/help')
    assert b'Your workspace owner and Twinevia support are handling activation.' in waiting_staff.data
    assert b'built-in method' not in waiting_owner.data and b'built-in method' not in waiting_staff.data
    assert f.m.ScheduledMessage.query.count() == 0
    assert f.provider.client.messages.create.call_count == 0


def test_help_query_is_bounded_escaped_and_redacted_for_staff(feedback):
    f = feedback
    query = '<script>alert("help-query")</script>' + 'x' * 120
    response = f.client.get('/help', query_string={'q': '  ' + query + '  '})
    parsed = HelpDocument(response)
    assert parsed.query == query[:100]
    assert b'<script>alert("help-query")</script>' not in response.data
    assert b'&lt;script&gt;' in response.data
    assert parsed.topics == [] and b'No Matching Help Topics' in response.data
    assert '/help' in parsed.links
    staff = f.staff_client.get('/help', query_string={'q': '+17205550145'})
    assert staff.status_code == 200 and b'+17205550145' not in staff.data
    assert '+17205550145' not in HelpDocument(staff).query
    matching = HelpDocument(f.client.get('/help', query_string={'q': '  CaRrIeR  '}))
    assert matching.query == 'CaRrIeR' and 'messaging-setup' in matching.topics


def test_staff_setup_and_help_share_readiness_without_owner_controls(feedback):
    f = feedback
    f.profile.provider_status = 'pending'
    f.db.session.commit()
    setup = f.staff_client.get('/setup?step=compliance')
    assert setup.status_code == 302 and setup.location.endswith('/setup/pending')
    waiting = f.staff_client.get('/setup/pending')
    parsed = HelpDocument(waiting)
    assert {'/dashboard', '/setup/pending', '/help'} <= set(parsed.links)
    assert b'Your workspace is available. Live messaging is waiting for setup to finish.' in waiting.data
    assert b'payment' not in waiting.data and b'name="action"' not in waiting.data
    assert b'name="declaration_accepted"' not in waiting.data
    workspace = f.staff_client.get('/dashboard')
    assert workspace.status_code == 200 and b'id="sendBtn" disabled' in workspace.data
    f.profile.provider_status = 'active'
    f.db.session.commit()
    ready = f.staff_client.get('/setup/pending')
    assert ready.status_code == 302 and ready.location.endswith('/dashboard')
    assert b'You can prepare a message and review its audience before sending.' in f.staff_client.get('/help').data
    assert f.provider.client.messages.create.call_count == 0


def test_staff_denied_settings_pages_keep_403_without_an_inaccessible_guide(feedback):
    f = feedback
    assert 'settings' not in HelpDocument(f.staff_client.get('/help')).topics
    for path in ('/settings', '/settings/event-sync?provider=app'):
        denied = f.staff_client.get(path)
        assert denied.status_code == 403
        assert b'This action requires a workspace owner.' in denied.data
        assert b'href="/dashboard"' in denied.data
        assert b'/help#settings' not in denied.data


def test_help_requires_login_and_platform_account_gets_platform_only_actions(feedback):
    f = feedback
    anonymous = f.app.test_client().get('/help')
    assert anonymous.status_code == 302 and '/login' in anonymous.location
    platform = f.m.AppUser(username='help-platform', email='help-platform@fixture.test',
        phone='+17205550913', role='admin', is_platform_admin=True)
    platform.set_password('Help-platform-pass1!')
    f.db.session.add(platform); f.db.session.commit()
    client = f.app.test_client()
    assert client.post('/platform/login', data={'username':platform.email,
        'password':'Help-platform-pass1!'}).status_code == 302
    response = client.get('/help')
    parsed = HelpDocument(response)
    assert b'Platform Admin' in response.data
    assert {'/platform/organizations', '/platform/pilot-applications'} <= set(parsed.links)
    assert not {'/dashboard', '/community', '/setup', '/billing', '/settings'} & set(parsed.links)
    assert all(topic.startswith('platform-') for topic in parsed.topics)
    assert b'data-context-help' not in response.data
    boundary = client.get('/dashboard')
    assert boundary.status_code == 302 and boundary.location.endswith('/platform')


def test_integration_and_log_detail_select_one_nav_item_and_the_correct_guide(feedback):
    f = feedback
    log = f.m.MessageLog(organization=f.org, event=f.event, target='event',
        message_body='Previously submitted message', details='[]')
    other_log = f.m.MessageLog(organization=f.other, event=f.other_event, target='event',
        message_body='Private other workspace message', details='[]')
    f.db.session.add_all([log, other_log]); f.db.session.commit()
    for client, path, expected, guide in [
        (f.client, '/settings/event-sync?provider=app', '/settings', '/help#settings'),
        (f.client, f'/logs/{log.id}', '/logs', '/help#message-history'),
        (f.staff_client, f'/logs/{log.id}', '/logs', '/help#message-history'),
    ]:
        parsed = HelpDocument(client.get(path))
        assert parsed.current_navigation == [[expected], [expected]]
        assert guide in parsed.links
        assert '/events' not in [link for region in parsed.current_navigation for link in region]
    assert f.client.get(f'/logs/{other_log.id}').status_code == 404
    assert f.staff_client.get('/settings/event-sync?provider=app').status_code == 403
