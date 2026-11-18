"""The full timer path stays quiet when there is no due work."""
from datetime import timedelta
import logging
from unittest.mock import Mock

from flask import Flask
import pytest

from app import _configure_scheduler, db
from app.models import Organization, ScheduledMessage, utc_now
from app.scheduler_runner import run_once


@pytest.fixture
def idle_app(tmp_path):
    app = Flask(__name__)
    app.config.update(TESTING=True, SAAS_MODE=True, SCHEDULER_ENABLED=False,
                      SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'idle.db'),
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        try:
            yield app
        finally:
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize('future_only', [False, True])
def test_real_idle_oneshot_emits_at_most_one_info_and_no_warning(idle_app, future_only, caplog):
    if future_only:
        organization = Organization(name='Future schedule workspace', slug='future-schedule')
        db.session.add(organization)
        db.session.flush()
        db.session.add(ScheduledMessage(organization_id=organization.id, message_body='Retained future message',
            target='community', status='pending', scheduled_at=utc_now().replace(tzinfo=None) + timedelta(days=1)))
        db.session.commit()
    with caplog.at_level(logging.INFO):
        _configure_scheduler(idle_app, start_scheduler=False)
        run_once(idle_app)
    assert len([record for record in caplog.records if record.levelno == logging.INFO]) <= 1
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    if future_only:
        assert ScheduledMessage.query.one().status == 'pending'


@pytest.mark.parametrize('active', [False, True])
def test_recovery_summaries_log_only_when_there_is_activity(idle_app, monkeypatch, caplog, active):
    outbound = {'requests_seen': int(active), 'requests_queued': 0}
    replies = {'seen': int(active), 'queued': 0, 'finalized': 0}
    recover_outbound = Mock(return_value=outbound)
    recover_replies = Mock(return_value=replies)
    schedule = Mock()
    monkeypatch.setattr('app.services.outbound_request_service.reconcile_outbound_requests', recover_outbound)
    monkeypatch.setattr('app.services.automation_outbox_service.recover_automation_replies', recover_replies)
    monkeypatch.setattr('app.services.scheduler_service.send_scheduled_messages', schedule)
    with caplog.at_level(logging.INFO):
        run_once(idle_app)
    assert len(caplog.records) == (2 if active else 0)
    if active:
        assert 'Outbound recovery' in caplog.text and 'Automatic reply recovery' in caplog.text
    recover_outbound.assert_called_once_with()
    recover_replies.assert_called_once_with()
    schedule.assert_called_once_with(idle_app)


def test_recovery_warning_is_not_suppressed(idle_app, monkeypatch, caplog):
    def recover():
        idle_app.logger.warning('Fixture recovery needs attention')
        return {'requests_seen': 1, 'requests_queued': 0}
    monkeypatch.setattr('app.services.outbound_request_service.reconcile_outbound_requests', recover)
    monkeypatch.setattr('app.services.automation_outbox_service.recover_automation_replies', lambda: {'seen': 0, 'queued': 0, 'finalized': 0})
    monkeypatch.setattr('app.services.scheduler_service.send_scheduled_messages', lambda app: None)
    with caplog.at_level(logging.INFO):
        run_once(idle_app)
    assert any(record.levelno == logging.WARNING and 'needs attention' in record.message for record in caplog.records)
    assert 'Outbound recovery' in caplog.text


def test_invalid_background_scheduler_configuration_still_warns(idle_app, caplog):
    idle_app.config['SCHEDULER_ENABLED'] = True
    with caplog.at_level(logging.INFO):
        _configure_scheduler(idle_app, start_scheduler=False)
    assert any(record.levelno == logging.WARNING and 'not started' in record.message for record in caplog.records)
