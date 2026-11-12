"""Audience-only previews expose grouped name metrics, never recipient text."""
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from app.utils import build_sms_audience_profile
from tests.test_composer_estimate import composer
from tests.test_event_feedback import feedback


def _profile(f, *, client=None, **fields):
    response = (client or f.client).post('/dashboard/audience-preview', data={
        'audience_profile': '1', 'target': 'community', **fields,
    }, headers={'Accept': 'application/json'})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


@pytest.mark.parametrize('name,first,full', [
    ('', (5, 5, False), (5, 5, False)),
    (' \t ', (5, 5, False), (5, 5, False)),
    ('A^ B', (3, 2, False), (5, 4, False)),
    ('A’B', (2, 3, True), (2, 3, True)),
    ('A 🙂', (1, 1, False), (2, 4, True)),
    ('🙂', (0, 2, True), (0, 2, True)),
])
def test_name_metrics_use_raw_rendered_fallback_gsm_extensions_and_utf16(name, first, full):
    profile = build_sms_audience_profile([{'name': name}])
    assert len(profile) == 1 and profile[0]['count'] == 1
    keys = ('gsm_units', 'utf16_units', 'unicode')
    assert profile[0]['first'] == dict(zip(keys, first))
    assert profile[0]['full'] == dict(zip(keys, full))


def test_profile_retains_joint_first_and_full_lengths_and_groups_identical_vectors():
    names = ['A BB', 'Z CC', 'A BBBBB', 'AA B', 'AA BBBB']
    profile = build_sms_audience_profile([{'name': name} for name in names])
    counts = {(row['first']['utf16_units'], row['full']['utf16_units']): row['count'] for row in profile}
    assert counts == {(1, 4): 2, (1, 7): 1, (2, 4): 1, (2, 7): 1}
    assert sum(row['count'] for row in profile) == len(names)
    assert build_sms_audience_profile(None) == build_sms_audience_profile([]) == []


def test_profile_calculation_does_not_parse_or_analyze_a_message(composer, monkeypatch):
    import app.routes as routes
    import app.utils as utils
    message_work = Mock(side_effect=AssertionError('Audience profiles must not analyze a draft.'))
    monkeypatch.setattr(routes, '_send_review_intent', message_work)
    monkeypatch.setattr(routes, 'analyze_personalized_sms_blast', message_work)
    monkeypatch.setattr(utils, 'analyze_sms_body', message_work)
    preview = _profile(composer, message_body='{unsupported}' * 200,
        schedule_later='on', schedule_date='not-a-date', schedule_time='not-a-time', include_unsubscribe='on')
    assert preview['eligible'] == 2 and preview['excluded'] == 2 and preview['total'] == 4
    assert preview['reasons']['unsubscribed'] == preview['reasons']['permission'] == 1
    message_work.assert_not_called()
    assert composer.m.OutboundRequest.query.count() == composer.m.MessageLog.query.count() == composer.m.ScheduledMessage.query.count() == 0


def test_profile_and_full_composition_share_audience_eligibility(composer):
    f = composer
    profile = _profile(f)
    response = f.client.post('/dashboard/audience-preview', data={
        'target': 'community', 'message_body': 'Hi {first_name} {full_name}',
    })
    assert response.status_code == 200
    composition = response.get_json()
    for key in ('eligible', 'excluded', 'total', 'reasons'):
        assert profile[key] == composition[key]
    assert profile['profile'] == build_sms_audience_profile([
        {'name': member.name} for member in f.members[:2]
    ])


def test_staff_profile_has_only_group_counts_and_metrics_without_contact_identifiers(composer):
    owner = _profile(composer)
    staff = _profile(composer, client=composer.staff_client)
    assert owner == staff
    assert set(staff) == {'eligible', 'excluded', 'total', 'reasons', 'profile'}
    for bucket in staff['profile']:
        assert set(bucket) == {'count', 'first', 'full'}
        assert type(bucket['count']) is int and bucket['count'] > 0
        for token in ('first', 'full'):
            assert set(bucket[token]) == {'gsm_units', 'utf16_units', 'unicode'}
            assert type(bucket[token]['gsm_units']) is type(bucket[token]['utf16_units']) is int
            assert type(bucket[token]['unicode']) is bool
    text = json.dumps(staff)
    for member in composer.members:
        assert member.name not in text and member.phone not in text
    for forbidden in ('phone', 'name', 'character', 'codepoint', 'sample', 'snapshot', 'review_token', 'recipient_id'):
        assert forbidden not in text


@pytest.mark.parametrize('mode', ['one', 'all'])
def test_test_recipient_profiles_match_for_owner_phone_and_staff_opaque_id(composer, mode):
    f = composer
    recipients = [f.m.OrganizationTestRecipient(organization_id=f.org.id, label=label, phone=f'+1720559{index:04d}')
                  for index, label in enumerate(['Al', 'Private 🙂 Label'])]
    f.db.session.add_all(recipients)
    f.db.session.commit()
    fields = {'test_mode': 'on', 'test_recipient_selection_mode': mode}
    owner = _profile(f, **fields, test_recipient_phone=recipients[0].phone if mode == 'one' else '')
    staff = _profile(f, client=f.staff_client, **fields, test_recipient_id=recipients[0].id)
    assert owner == staff and owner['eligible'] == (1 if mode == 'one' else 2)
    assert sum(row['count'] for row in owner['profile']) == owner['eligible']
    text = json.dumps(staff)
    for recipient in recipients:
        assert recipient.phone not in text and recipient.label not in text


def test_staff_test_recipient_profile_rejects_raw_phone_and_other_tenant_id(composer):
    f = composer
    foreign = f.m.OrganizationTestRecipient(organization_id=f.other.id, label='Other private test contact', phone='+17205589998')
    f.db.session.add(foreign)
    f.db.session.commit()
    fields = {'audience_profile': '1', 'test_mode': 'on', 'test_recipient_selection_mode': 'one'}
    for selection in ({'test_recipient_phone': foreign.phone}, {'test_recipient_id': foreign.id}):
        response = f.staff_client.post('/dashboard/audience-preview', data={**fields, **selection})
        assert response.status_code == 400 and response.is_json
        assert foreign.phone not in response.get_data(as_text=True)


def test_event_profile_keeps_tenant_permission_and_empty_audience_rules(composer):
    f = composer
    assert _profile(f, target='event', event_id=f.event.id)['profile'] == []
    from app.services.recipient_permission_service import record_permission_evidence
    registration = f.m.EventRegistration(organization_id=f.org.id, event=f.event, name='Event profile guest', phone='+17205589001')
    f.db.session.add(registration)
    f.db.session.flush()
    with f.organization_context(f.org.id):
        record_permission_evidence(f.org.id, registration.phone, purpose='event', event_id=f.event.id,
            decision='granted', source='profile_fixture', evidence_key='profile-event-guest', verified=True,
            metadata={'proof': 'Synthetic audience profile permission.'})
    f.db.session.commit()
    preview = _profile(f, target='event', event_id=f.event.id)
    assert preview['eligible'] == 1 and preview['profile'] == build_sms_audience_profile([{'name': registration.name}])
    assert f.client.post('/dashboard/audience-preview', data={
        'audience_profile': '1', 'target': 'event', 'event_id': f.other_event.id,
    }).status_code == 404


@pytest.mark.parametrize('fields', [
    {'target': 'other'}, {'target': 'event'}, {'target': 'event', 'event_id': 'invalid'},
])
def test_profile_rejects_invalid_audience_without_requiring_message_fields(composer, fields):
    response = composer.client.post('/dashboard/audience-preview', data={'audience_profile': '1', **fields})
    assert response.status_code == 400 and response.is_json and response.get_json()['error']


def _browser_estimate(body, recipients, *, footer=''):
    library = str(Path(__file__).resolve().parents[1] / 'app/static/js/sms_analysis.js')
    script = """
global.window = {};
require(process.argv[1]);
const input = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(window.TwineviaSmsAnalysis.estimateAudience(
  input.body, input.profile, {footer: input.footer}
)));
"""
    # The browser receives aggregate lengths and flags, never these names.
    payload = {'body': body, 'profile': build_sms_audience_profile(recipients), 'footer': footer}
    result = subprocess.run(['node', '-e', script, library, json.dumps(payload)],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('body,names,footer', [
    ('x' * 152 + '{first_name}{full_name}', ['A BBBBB', 'AA BBB', 'Z CCCCC'], ''),
    ('x' * 140 + '{FIRST_NAME} {first_name} {NaMe} {full_name}', ['A BB', 'Alexandria Cassandra'], ''),
    ('Hello {first_name} {NAME}', ['', None, ' \t '], ''),
    ('x' * 152 + '{first_name}', ['^ Extension', 'Plain Armenianա'], ''),
    ('x' * 65 + '{full_name}', ['^ Extension', 'Plain ա'], ''),
    ('🙂' * 34 + 'x{first_name}', ['^ Alpha', '🙂 Other', 'A^ Beta', '’ Gamma'], ''),
    ('x' * 64 + '{first_name}', ['One’Two'], ''),
    ('  Hi — {FIRST_NAME}…\u202f  ', ['One’Two', 'Plain Name'], ''),
    ('x' * 143 + '{first_name}', ['Al', 'Alexandria Cassandra'], '\n\nReply STOP to unsubscribe.'),
    ('Hello {first_name} 🙂', [], '\n\nReply STOP to unsubscribe.'),
    ('A' * 70 + '\uFEFF', ['Private Name'], ''),
    ('A' * 160 + '\u0085', ['Private Name'], ''),
    ('\u001C\u001D\u001E\u001F' + 'A' * 160 + '\u0085', ['Private Name'], ''),
])
def test_browser_aggregate_estimate_matches_authoritative_personalization(body, names, footer):
    from app.utils import analyze_personalized_sms_blast, normalize_sms_body
    recipients = [{'name': name} for name in names]
    expected = analyze_personalized_sms_blast(normalize_sms_body(body.strip()) + footer, recipients)
    actual = _browser_estimate(body, recipients, footer=footer)
    for key in ('min_segment_count', 'max_segment_count', 'total_segments', 'encodings'):
        assert actual[key] == expected[key], key
    assert 'per_recipient' not in actual
    assert not any(key in actual for key in ('name', 'phone', 'sample', 'recipient_id'))


@pytest.mark.parametrize('body', ['', ' \n\t '])
def test_empty_browser_draft_has_no_estimated_segments_even_with_a_footer(body):
    actual = _browser_estimate(body, [{'name': 'Private Contact'}, {'name': 'Another Contact'}],
                              footer='\n\nReply STOP to unsubscribe.')
    assert {key: actual[key] for key in (
        'min_segment_count', 'max_segment_count', 'total_segments', 'encodings',
    )} == {'min_segment_count': 0, 'max_segment_count': 0, 'total_segments': 0, 'encodings': []}
