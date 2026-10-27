"""Keyword edits and legacy whitespace use the same tenant-scoped intent."""
import importlib
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text


@pytest.fixture
def keywords(tmp_path, monkeypatch):
    root = os.environ.get('KEYWORD_TEST_POSTGRES_ROOT')
    database_name = None
    if root:
        if root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('Keyword tests require the dedicated loopback PostgreSQL audit root.')
        import psycopg
        from psycopg import sql
        database_name = 'keyword_edit_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'keywords.db')
    for key, value in {
        'DATABASE_URL': uri, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1', 'SCHEDULER_ENABLED': '0',
        'PYTHON_DOTENV_DISABLED': '1', 'SECRET_KEY': 'isolated-keyword-edits', 'AUTH_ALERTS_ENABLED': '0',
        'REDIS_URL': 'redis://127.0.0.1:1/0',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db, models
    from app.tenant import organization_context
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    try:
        with app.app_context():
            db.create_all()
            org = models.Organization(name='Keyword workspace', slug='keyword-workspace')
            other = models.Organization(name='Independent keywords', slug='independent-keywords')
            owner = models.AppUser(username='keyword-owner', email='keyword@fixture.test',
                phone='+17205550871', role='admin', must_change_password=False)
            owner.set_password('Keyword-fixture-pass1!')
            db.session.add_all([org, other, owner])
            db.session.flush()
            db.session.add_all([
                models.OrganizationMembership(organization_id=org.id, user_id=owner.id, role='owner'),
                models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMessagingProfile(organization=org, status='active', provider_status='active',
                    sender_review_status='approved', from_number='+17205550879', phone_number_sid='PNkeywordfixture',
                    twilio_subaccount_sid='ACkeywordfixture', messaging_service_sid='MGkeywordfixture'),
            ])
            for policy, key in [('terms','TERMS_POLICY_VERSION'),('privacy','PRIVACY_POLICY_VERSION'),
                    ('acceptable_use','ACCEPTABLE_USE_POLICY_VERSION'),('sms','SMS_POLICY_VERSION'),('billing','BILLING_POLICY_VERSION')]:
                db.session.add(models.CustomerPolicyAcceptance(organization_id=org.id, user_id=owner.id,
                    policy_name=policy, policy_version=app.config[key]))
            db.session.commit()
            client = app.test_client()
            login = client.post('/login', data={'username':owner.email, 'password':'Keyword-fixture-pass1!'})
            assert login.status_code == 302
            yield SimpleNamespace(app=app, db=db, m=models, org=org, other=other, owner=owner,
                                  client=client, organization_context=organization_context)
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database_name)))


def rule(keywords, value='JOIN', *, org=None):
    row = keywords.m.KeywordAutomationRule(organization_id=(org or keywords.org).id,
        keyword=value, response_body='Original keyword response', is_active=True)
    keywords.db.session.add(row)
    keywords.db.session.commit()
    return row


def survey(keywords, value='JOIN', *, org=None):
    row = keywords.m.SurveyFlow(organization_id=(org or keywords.org).id,
        name='Survey ' + uuid4().hex, trigger_keyword=value, is_active=True)
    row.set_questions(['Original question?'])
    keywords.db.session.add(row)
    keywords.db.session.commit()
    return row


def edit_survey(keywords, row, *, trigger='JOIN', questions='Updated question?', is_active=True):
    data = {
        'name':row.name, 'trigger_keyword':trigger, 'questions':questions,
        'completion_message':'Thanks for the feedback', 'event_link_mode':'none',
    }
    if is_active:
        data['is_active'] = 'on'
    return keywords.client.post(f'/inbox/surveys/{row.id}/edit', data=data)


@pytest.mark.parametrize('raw', ['\tJoin\n', ' \t  hello\r\nworld\f\v ', ' '*16 + 'join' + '\n'*8, '\r\n'])
def test_sql_legacy_control_whitespace_matches_application_normalization(keywords, raw):
    from app.routes import _normalized_keyword_sql
    from app.utils import normalize_keyword
    assert keywords.db.session.query(_normalized_keyword_sql(keywords.db.literal(raw))).scalar() == normalize_keyword(raw)


@pytest.mark.parametrize('kind', ['rule', 'survey'])
def test_edge_control_whitespace_cannot_hide_a_new_keyword_collision(keywords, kind):
    from app.routes import _keyword_conflicts_with_rule, _keyword_conflicts_with_survey
    row = rule(keywords) if kind == 'rule' else survey(keywords)
    table, column = ('keyword_automation_rules', 'keyword') if kind == 'rule' else ('survey_flows', 'trigger_keyword')
    keywords.db.session.execute(text(f'UPDATE {table} SET {column}=:value WHERE id=:id'),
        {'value':'\t join\r\n', 'id':row.id})
    keywords.db.session.commit()
    with keywords.organization_context(keywords.org.id):
        check = _keyword_conflicts_with_rule if kind == 'rule' else _keyword_conflicts_with_survey
        assert check('JOIN') is True
    response = keywords.client.post('/inbox/keywords/add', data={
        'keyword':'JOIN', 'response_body':'A different automatic response', 'is_active':'on',
    })
    assert response.status_code == 200
    assert b'already' in response.data
    assert keywords.m.KeywordAutomationRule.query.count() == (1 if kind == 'rule' else 0)


@pytest.mark.parametrize('kind', ['rule', 'survey'])
def test_existing_cross_table_overlap_allows_nontrigger_edits(keywords, kind):
    saved_rule, saved_survey = rule(keywords), survey(keywords)
    if kind == 'rule':
        response = keywords.client.post(f'/inbox/keywords/{saved_rule.id}/edit', data={
            'keyword':' join ', 'response_body':'Updated response without a new trigger', 'is_active':'on',
        })
        assert response.status_code == 302
        keywords.db.session.refresh(saved_rule)
        assert saved_rule.response_body == 'Updated response without a new trigger'
    else:
        response = edit_survey(keywords, saved_survey, trigger=' join ')
        assert response.status_code == 302
        keywords.db.session.refresh(saved_survey)
        assert saved_survey.questions == ['Updated question?']
    assert saved_rule.keyword == saved_survey.trigger_keyword == 'JOIN'


@pytest.mark.parametrize('kind', ['rule', 'survey'])
def test_unchanged_normalized_legacy_trigger_is_not_silently_rewritten(keywords, kind):
    saved_rule, saved_survey = rule(keywords), survey(keywords)
    row = saved_rule if kind == 'rule' else saved_survey
    table, column = ('keyword_automation_rules', 'keyword') if kind == 'rule' else ('survey_flows', 'trigger_keyword')
    raw = '\tJOIN\n'
    keywords.db.session.execute(text(f'UPDATE {table} SET {column}=:value WHERE id=:id'), {'value':raw, 'id':row.id})
    keywords.db.session.commit()
    keywords.db.session.expire_all()
    if kind == 'rule':
        response = keywords.client.post(f'/inbox/keywords/{saved_rule.id}/edit', data={
            'keyword':'JOIN', 'response_body':'Only the response changed', 'is_active':'on',
        })
    else:
        response = edit_survey(keywords, saved_survey)
    assert response.status_code == 302
    keywords.db.session.refresh(row)
    assert getattr(row, column) == raw


@pytest.mark.parametrize('kind', ['rule', 'survey'])
@pytest.mark.parametrize('collision_table', ['same', 'other'])
def test_new_or_changed_trigger_still_rejects_collisions(keywords, kind, collision_table):
    target = rule(keywords, 'ORIGINAL') if kind == 'rule' else survey(keywords, 'ORIGINAL')
    occupied_kind = kind if collision_table == 'same' else ('survey' if kind == 'rule' else 'rule')
    occupied = rule(keywords, 'TAKEN') if occupied_kind == 'rule' else survey(keywords, 'TAKEN')
    if kind == 'rule':
        response = keywords.client.post(f'/inbox/keywords/{target.id}/edit', data={
            'keyword':' taken ', 'response_body':'Do not save this collision', 'is_active':'on',
        })
    else:
        response = edit_survey(keywords, target, trigger=' taken ')
    assert response.status_code == 200
    assert b'already' in response.data
    keywords.db.session.refresh(target)
    assert (target.keyword if kind == 'rule' else target.trigger_keyword) == 'ORIGINAL'
    assert occupied is not None


def test_keyword_collision_checks_remain_tenant_scoped_and_ids_are_not_cross_table_exclusions(keywords):
    from app.routes import _keyword_conflicts_with_survey
    foreign = survey(keywords, 'FOREIGN', org=keywords.other)
    with keywords.organization_context(keywords.org.id):
        assert _keyword_conflicts_with_survey('FOREIGN') is False
    rule(keywords, 'FIRST')
    response = keywords.client.post('/inbox/keywords/add', data={
        'keyword':'FOREIGN', 'response_body':'Local tenant response', 'is_active':'on',
    })
    assert response.status_code == 302
    # Independent integer sequences routinely give rule and survey the same ID.
    local_rule = keywords.m.KeywordAutomationRule.query.filter_by(organization_id=keywords.org.id, keyword='FOREIGN').one()
    local_survey = survey(keywords, 'TAKEN')
    assert foreign.id != local_survey.id
    assert local_rule.id == local_survey.id
    response = keywords.client.post(f'/inbox/keywords/{local_rule.id}/edit', data={
        'keyword':'TAKEN', 'response_body':'Cross-table ID equality cannot waive a conflict', 'is_active':'on',
    })
    assert response.status_code == 200
    keywords.db.session.refresh(local_rule)
    assert local_rule.keyword == 'FOREIGN'


@pytest.mark.parametrize('kind', ['rule', 'survey'])
def test_legacy_overlap_can_be_disabled_but_cannot_be_reactivated_without_resolving_it(keywords, kind):
    saved_rule, saved_survey = rule(keywords), survey(keywords)
    if kind == 'rule':
        target = saved_rule
        response = keywords.client.post(f'/inbox/keywords/{target.id}/edit', data={
            'keyword':'JOIN', 'response_body':'Disabled legacy rule',
        })
    else:
        target = saved_survey
        response = edit_survey(keywords, target, is_active=False)
    assert response.status_code == 302
    keywords.db.session.refresh(target)
    assert target.is_active is False
    if kind == 'rule':
        response = keywords.client.post(f'/inbox/keywords/{target.id}/edit', data={
            'keyword':'JOIN', 'response_body':'Must not reintroduce an active conflict', 'is_active':'on',
        })
    else:
        response = edit_survey(keywords, target)
    assert response.status_code == 200
    assert b'already' in response.data
    keywords.db.session.refresh(target)
    assert target.is_active is False


@pytest.mark.parametrize('kind', ['rule', 'survey'])
def test_existing_overlap_can_be_resolved_by_a_new_unoccupied_trigger(keywords, kind):
    saved_rule, saved_survey = rule(keywords), survey(keywords)
    if kind == 'rule':
        target = saved_rule
        response = keywords.client.post(f'/inbox/keywords/{target.id}/edit', data={
            'keyword':'  move\t me  ', 'response_body':'Trigger conflict resolved', 'is_active':'on',
        })
    else:
        target = saved_survey
        response = edit_survey(keywords, target, trigger='  move\t me  ')
    assert response.status_code == 302
    keywords.db.session.refresh(target)
    assert (target.keyword if kind == 'rule' else target.trigger_keyword) == 'MOVE ME'
