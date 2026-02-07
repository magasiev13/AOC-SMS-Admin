import json
from datetime import datetime as dt, timezone

from flask_login import UserMixin
from sqlalchemy.orm import validates
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.utils import normalize_keyword, normalize_phone


def utc_now():
    return dt.now(timezone.utc)


class AppUser(UserMixin, db.Model):
    """Application users with role-based access."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default='admin')
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    @property
    def is_social_manager(self) -> bool:
        return self.role == 'social_manager'

    def __repr__(self):
        return f'<AppUser {self.username} role={self.role}>'


class CommunityMember(db.Model):
    """Recipients for community-wide SMS blasts."""
    __tablename__ = 'community_members'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    def __repr__(self):
        return f'<CommunityMember {self.phone}>'


class UnsubscribedContact(db.Model):
    """Phone numbers that should not receive messages."""
    __tablename__ = 'unsubscribed_contacts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False, unique=True)
    reason = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(50), nullable=False, default='manual')
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<UnsubscribedContact {self.phone}>'


class SuppressedContact(db.Model):
    """Phone numbers that should not receive messages for specific reasons."""
    __tablename__ = 'suppressed_contacts'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, unique=True)
    reason = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(50), nullable=True)
    source_type = db.Column(db.String(50), nullable=True)
    source_message_log_id = db.Column(db.Integer, db.ForeignKey('message_logs.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    message_log = db.relationship('MessageLog')

    @validates('phone')
    def _normalize_phone(self, key, value):
        return normalize_phone(value)

    def __repr__(self):
        return f'<SuppressedContact {self.phone} category={self.category}>'


class Event(db.Model):
    """Event definitions."""
    __tablename__ = 'events'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    registrations = db.relationship('EventRegistration', back_populates='event', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Event {self.title}>'


class EventRegistration(db.Model):
    """Recipients registered for a specific event (separate from community members)."""
    __tablename__ = 'event_registrations'
    
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    event = db.relationship('Event', back_populates='registrations')
    
    __table_args__ = (db.UniqueConstraint('event_id', 'phone', name='unique_event_phone'),)
    
    def __repr__(self):
        return f'<EventRegistration event={self.event_id} phone={self.phone}>'


class MessageLog(db.Model):
    """Log of sent SMS blasts."""
    __tablename__ = 'message_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    message_body = db.Column(db.Text, nullable=False)
    target = db.Column(db.String(20), nullable=False)  # 'community' or 'event'
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True)
    status = db.Column(db.String(20), default='sent')  # 'processing', 'sent', 'failed'
    total_recipients = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    details = db.Column(db.Text, nullable=True)  # JSON string of per-recipient results
    
    event = db.relationship('Event')
    
    def __repr__(self):
        return f'<MessageLog {self.id} target={self.target}>'


class InboxThread(db.Model):
    """Conversation threads grouped by phone number."""
    __tablename__ = 'inbox_threads'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, unique=True, index=True)
    contact_name = db.Column(db.String(100), nullable=True)
    unread_count = db.Column(db.Integer, default=0, nullable=False)
    last_message_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)
    last_message_preview = db.Column(db.Text, nullable=True)
    last_direction = db.Column(db.String(10), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    messages = db.relationship(
        'InboxMessage',
        back_populates='thread',
        cascade='all, delete-orphan',
        order_by='InboxMessage.created_at',
    )

    def __repr__(self):
        return f'<InboxThread {self.phone}>'


class InboxMessage(db.Model):
    """Inbound and outbound messages shown in the shared inbox."""
    __tablename__ = 'inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('inbox_threads.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # inbound/outbound
    body = db.Column(db.Text, nullable=False)
    message_sid = db.Column(db.String(64), nullable=True, unique=True)
    automation_source = db.Column(db.String(30), nullable=True)
    automation_source_id = db.Column(db.Integer, nullable=True)
    matched_keyword = db.Column(db.String(40), nullable=True)
    delivery_status = db.Column(db.String(30), nullable=True)
    delivery_error = db.Column(db.Text, nullable=True)
    raw_payload = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    thread = db.relationship('InboxThread', back_populates='messages')

    def __repr__(self):
        return f'<InboxMessage {self.id} {self.direction} {self.phone}>'


class KeywordAutomationRule(db.Model):
    """Keyword-based automated replies for inbound SMS."""
    __tablename__ = 'keyword_automation_rules'

    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(40), nullable=False, unique=True, index=True)
    response_body = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    match_count = db.Column(db.Integer, default=0, nullable=False)
    last_matched_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    @validates('keyword')
    def _normalize_keyword(self, key, value):
        return normalize_keyword(value)

    def __repr__(self):
        return f'<KeywordAutomationRule {self.keyword}>'


class SurveyFlow(db.Model):
    """Multi-step inbound survey started by a keyword."""
    __tablename__ = 'survey_flows'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    trigger_keyword = db.Column(db.String(40), nullable=False, unique=True, index=True)
    intro_message = db.Column(db.Text, nullable=True)
    questions_json = db.Column(db.Text, nullable=False, default='[]')
    completion_message = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    start_count = db.Column(db.Integer, default=0, nullable=False)
    completion_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    sessions = db.relationship('SurveySession', back_populates='survey')
    responses = db.relationship('SurveyResponse', back_populates='survey')

    @validates('trigger_keyword')
    def _normalize_trigger_keyword(self, key, value):
        return normalize_keyword(value)

    @property
    def questions(self) -> list[str]:
        try:
            payload = json.loads(self.questions_json or '[]')
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    def set_questions(self, questions: list[str]) -> None:
        self.questions_json = json.dumps([question.strip() for question in questions if question and question.strip()])

    def __repr__(self):
        return f'<SurveyFlow {self.name} keyword={self.trigger_keyword}>'


class SurveySession(db.Model):
    """Per-phone active/completed survey progress."""
    __tablename__ = 'survey_sessions'

    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey_flows.id'), nullable=False, index=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('inbox_threads.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    status = db.Column(db.String(20), default='active', nullable=False)  # active/completed/cancelled
    current_question_index = db.Column(db.Integer, default=0, nullable=False)
    started_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_activity_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_survey_sessions_phone_status', 'phone', 'status'),
    )

    survey = db.relationship('SurveyFlow', back_populates='sessions')
    thread = db.relationship('InboxThread')
    responses = db.relationship(
        'SurveyResponse',
        back_populates='session',
        cascade='all, delete-orphan',
        order_by='SurveyResponse.question_index',
    )

    def __repr__(self):
        return f'<SurveySession survey={self.survey_id} phone={self.phone} status={self.status}>'


class SurveyResponse(db.Model):
    """Captured answer for one survey question."""
    __tablename__ = 'survey_responses'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('survey_sessions.id'), nullable=False, index=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey_flows.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    question_index = db.Column(db.Integer, nullable=False)
    question_prompt = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    session = db.relationship('SurveySession', back_populates='responses')
    survey = db.relationship('SurveyFlow', back_populates='responses')

    def __repr__(self):
        return f'<SurveyResponse session={self.session_id} q={self.question_index}>'


class ScheduledMessage(db.Model):
    """Scheduled SMS blasts for future sending."""
    __tablename__ = 'scheduled_messages'
    
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    message_body = db.Column(db.Text, nullable=False)
    target = db.Column(db.String(20), nullable=False)  # 'community' or 'event'
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'sent', 'failed', 'expired', 'cancelled'
    test_mode = db.Column(db.Boolean, default=False)  # If true, send only to admin test phone
    processing_started_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    message_log_id = db.Column(db.Integer, db.ForeignKey('message_logs.id'), nullable=True)
    
    event = db.relationship('Event')
    message_log = db.relationship('MessageLog')
    
    def __repr__(self):
        return f'<ScheduledMessage {self.id} scheduled={self.scheduled_at} status={self.status}>'


class LoginAttempt(db.Model):
    """Track failed login attempts for rate limiting across workers."""
    __tablename__ = 'login_attempts'

    id = db.Column(db.Integer, primary_key=True)
    client_ip = db.Column(db.String(45), nullable=False, index=True)
    attempt_count = db.Column(db.Integer, default=1, nullable=False)
    first_attempt_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<LoginAttempt {self.client_ip} count={self.attempt_count}>'
