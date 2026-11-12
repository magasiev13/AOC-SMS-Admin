"""Composer and review share recipient-aware SMS estimates without sending."""
import json
from pathlib import Path
import subprocess

import pytest

from tests.test_event_feedback import feedback


@pytest.fixture
def composer(feedback):
    f = feedback
    from app.services.recipient_permission_service import record_permission_evidence
    with f.organization_context(f.org.id):
        members = [f.m.CommunityMember(organization_id=f.org.id, name=name, phone=f'+1720558{index:04d}')
                   for index, name in enumerate(['Al', 'Alexandria Cassandra', 'Opted Out', 'No Permission'])]
        f.db.session.add_all(members)
        f.db.session.flush()
        for index, member in enumerate(members[:3]):
            record_permission_evidence(f.org.id, member.phone, purpose='community', decision='granted',
                source='composer_fixture', evidence_key=f'grant-{index}', verified=True,
                metadata={'proof': 'Synthetic composer permission.'})
        f.db.session.add(f.m.UnsubscribedContact(organization_id=f.org.id, phone=members[2].phone))
        f.db.session.add(f.m.CommunityMember(organization_id=f.other.id, name='Foreign long private name', phone='+17205589999'))
        f.db.session.commit()
    f.members = members
    return f


def _fields(**changes):
    return {'message_body': 'Hello {first_name}, ' + 'x' * 145, 'target': 'community',
            'request_idempotency_key': 'composer-estimate-fixture', **changes}


def _preview(f, fields, client=None):
    response = (client or f.client).post('/dashboard/audience-preview', data=fields, headers={'Accept': 'application/json'})
    assert response.status_code == 200
    return response.get_json()


@pytest.mark.parametrize('footer,total,minimum,maximum', [('', 3, 1, 2), ('on', 4, 2, 2)])
def test_personalized_estimate_matches_review_and_footer(composer, footer, total, minimum, maximum):
    f = composer
    fields = _fields(include_unsubscribe=footer)
    preview = _preview(f, fields)
    review = f.client.post('/dashboard/review', data=fields).get_json()
    assert preview['eligible'] == review['eligible'] == 2
    assert preview['excluded'] == review['excluded'] == 2
    assert preview['reasons']['unsubscribed'] == 1 and preview['reasons']['permission'] == 1
    assert preview['estimate']['total_segments'] == review['segments'] == total
    assert preview['estimate']['min_segment_count'] == minimum
    assert preview['estimate']['max_segment_count'] == maximum
    assert f.m.MessageLog.query.count() == f.m.OutboundRequest.query.count() == f.m.ScheduledMessage.query.count() == 0


def test_named_emoji_warning_and_astral_segments_use_the_review_calculation(composer):
    f = composer
    fields = _fields(message_body='🙂' * 36)
    preview = _preview(f, fields)
    review = f.client.post('/dashboard/review', data=fields).get_json()
    assert preview['estimate']['total_segments'] == review['segments'] == 4
    assert preview['estimate']['special_character'] == {
        'character': '🙂', 'codepoint': 'U+1F642', 'name': 'SLIGHTLY SMILING FACE',
    }
    assert review['sample'] == fields['message_body']
    assert preview['estimate']['encodings'] == ['ucs-2']


def test_existing_normalization_and_schedule_intent_are_unchanged(composer):
    f = composer
    f.app.config['APP_TIMEZONE'] = 'America/Denver'
    from app.services.send_review_service import reviewed_recipients
    from app.routes import _send_review_intent
    from werkzeug.datastructures import MultiDict
    fields = _fields(message_body='Hi {first_name} — same reminder…', include_unsubscribe='on',
        schedule_later='on', schedule_date='2030-12-04', schedule_time='10:37', client_timezone='America/Denver')
    preview = _preview(f, fields)
    review = f.client.post('/dashboard/review', data=fields).get_json()
    assert preview['estimate']['total_segments'] == review['segments']
    assert review['sample'] == 'Hi Al - same reminder...\n\nReply STOP to unsubscribe.'
    assert '2030' in review['timing'] and '10:37' in review['timing']
    with f.app.test_request_context('/dashboard'), f.organization_context(f.org.id):
        intent = _send_review_intent(MultiDict(fields))
        recipients = reviewed_recipients(review['review_token'], f.owner, f.org.id, fields['request_idempotency_key'], intent)
    assert [row['phone'] for row in recipients] == [row.phone for row in f.members[:2]]
    assert intent['schedule_date'] == '2030-12-04' and intent['schedule_time'] == '10:37'
    assert f.m.ScheduledMessage.query.count() == 0


def test_staff_preview_contains_only_aggregates_not_names_numbers_or_review_token(composer):
    f = composer
    fields = _fields()
    owner = _preview(f, fields)
    staff = _preview(f, fields, f.staff_client)
    assert staff == owner
    assert set(staff) == {'eligible', 'excluded', 'total', 'reasons', 'estimate'}
    assert set(staff['estimate']) == {'min_segment_count', 'max_segment_count', 'total_segments', 'encodings', 'special_character'}
    text = json.dumps(staff)
    for member in f.members:
        assert member.phone not in text and member.name not in text
    for forbidden in ['per_recipient', 'review_token', 'snapshot', 'phone', 'Foreign long private name']:
        assert forbidden not in text


@pytest.mark.parametrize('mode', ['all', 'one'])
def test_saved_test_recipient_estimate_matches_review_for_owner_and_staff(composer, mode):
    f = composer
    recipients = [f.m.OrganizationTestRecipient(organization_id=f.org.id, label=label, phone=f'+1720559{index:04d}')
                  for index, label in enumerate(['Al', 'Alexandria'])]
    f.db.session.add_all(recipients)
    f.db.session.commit()
    fields = _fields(test_mode='on', test_recipient_selection_mode=mode,
        test_recipient_phone=recipients[0].phone if mode == 'one' else '')
    owner = _preview(f, fields)
    owner_review = f.client.post('/dashboard/review', data=fields).get_json()
    assert owner['estimate']['total_segments'] == owner_review['segments']
    assert owner['eligible'] == (2 if mode == 'all' else 1)
    fields.pop('test_recipient_phone')
    fields['test_recipient_id'] = recipients[0].id
    staff = _preview(f, fields, f.staff_client)
    staff_review = f.staff_client.post('/dashboard/review', data=fields).get_json()
    assert staff['estimate']['total_segments'] == staff_review['segments'] == owner_review['segments']
    assert recipients[0].phone not in json.dumps(staff)


def test_event_preview_checks_exact_tenant_and_event_permission(composer):
    f = composer
    from app.services.recipient_permission_service import record_permission_evidence
    registration = f.m.EventRegistration(organization_id=f.org.id, event=f.event, name='Event guest', phone='+17205589001')
    f.db.session.add(registration)
    f.db.session.flush()
    with f.organization_context(f.org.id):
        record_permission_evidence(f.org.id, registration.phone, purpose='event', event_id=f.event.id,
            decision='granted', source='composer_fixture', evidence_key='event-guest', verified=True,
            metadata={'proof': 'Synthetic event permission.'})
    f.db.session.commit()
    fields = _fields(target='event', event_id=f.event.id)
    preview = _preview(f, fields)
    assert preview['eligible'] == 1 and preview['estimate']['total_segments'] == f.client.post('/dashboard/review', data=fields).get_json()['segments']
    assert f.client.post('/dashboard/audience-preview', data=_fields(target='event', event_id=f.other_event.id)).status_code == 404
    assert f.client.get('/dashboard/audience-preview?target=community').get_json()['eligible'] == 2


@pytest.mark.parametrize('changes', [
    {'message_body': ''}, {'message_body': '{unsupported}'}, {'target': 'other'},
    {'target': 'event', 'event_id': 'invalid'}, {'target': 'event'},
])
def test_invalid_compositions_return_json_without_creating_work(composer, changes):
    response = composer.client.post('/dashboard/audience-preview', data=_fields(**changes), headers={'Accept': 'application/json'})
    assert response.status_code == 400 and response.is_json and response.get_json()['error']
    assert composer.m.OutboundRequest.query.count() == composer.m.MessageLog.query.count() == 0


@pytest.mark.parametrize('body,units,segments', [
    ('🙂' * 35, 70, 1), ('🙂' * 36, 72, 2), ('🙂' * 67, 134, 2), ('🙂' * 68, 136, 3),
    ('a' * 68 + '🙂', 70, 1), ('a' * 69 + '🙂', 71, 2), ('ա' * 71, 71, 2),
    ('^' * 80, 160, 1), ('^' * 81, 162, 2),
])
def test_segment_boundaries_use_utf16_units_for_unicode(body, units, segments):
    from app.utils import analyze_sms_body
    result = analyze_sms_body(body, apply_normalization=False)
    assert result['characters_used'] == units and result['segment_count'] == segments


def test_browser_metrics_match_server_and_replacement_is_explicit():
    from app.utils import analyze_sms_body
    library = str(Path(__file__).resolve().parents[1] / 'app/static/js/sms_analysis.js')
    bodies = ['🙂' * 35, '🙂' * 36, 'a' * 69 + '🙂', '^' * 81, 'ա' * 71, 'Hi — 🙂']
    script = """
global.window = {};
require(process.argv[1]);
const inputs = JSON.parse(process.argv[2]);
const api = window.TwineviaSmsAnalysis;
process.stdout.write(JSON.stringify({
  metrics: inputs.map(body => api.analyzeBody(body)),
  replacement: api.replaceSpecialCharacters('Hi — 🙂'),
  special: api.specialCharacter('Hi 🙂'),
  originals: inputs,
}));
"""
    result = subprocess.run(['node', '-e', script, library, json.dumps(bodies)], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    for body, actual in zip(bodies, data['metrics']):
        expected = analyze_sms_body(body)
        assert actual['segmentCount'] == expected['segment_count']
        assert actual['charactersUsed'] == expected['characters_used']
        assert actual['encoding'] == expected['encoding']
    assert data['originals'] == bodies
    assert data['replacement'] == 'Hi -'
    assert data['special'] == {'character': '🙂', 'codepoint': 'U+1F642', 'name': 'SLIGHTLY SMILING FACE'}


@pytest.mark.parametrize('body,expected,unicode_writing', [
    ('🙂🎉👨‍👩‍👧‍👦🇦🇲1️⃣', '', False),
    ('Hello — 🙂  world…', 'Hello - world...', False),
    ('Բարև աշխարհ 🙂 — հանդիպում', 'Բարև աշխարհ 🙂 — հանդիպում', True),
    ('Hello ա 🙂', 'Hello ա', False),
    ('Hello everyone क्\u200dष 🙂', 'Hello everyone क्\u200dष', False),
    ('Written selector A\ufe0f 🙂', 'Written selector A\ufe0f', False),
    ('Emoji 👩🏽‍💻 🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f 2️⃣ end', 'Emoji end', False),
])
def test_special_character_replacement_removes_pictures_and_preserves_written_language(body, expected, unicode_writing):
    library = str(Path(__file__).resolve().parents[1] / 'app/static/js/sms_analysis.js')
    script = """
global.window = {};
require(process.argv[1]);
const body = process.argv[2];
process.stdout.write(JSON.stringify({
  result: window.TwineviaSmsAnalysis.replaceSpecialCharacters(body),
  unicodeWriting: window.TwineviaSmsAnalysis.usesUnicodeWriting(body),
}));
"""
    result = subprocess.run(['node', '-e', script, library, body], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {'result': expected, 'unicodeWriting': unicode_writing}
