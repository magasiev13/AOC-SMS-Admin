from datetime import datetime as dt, timezone

from sqlalchemy.orm import validates
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from app import db
from app.utils import normalize_phone


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
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    message_log_id = db.Column(db.Integer, db.ForeignKey('message_logs.id'), nullable=True)
    
    event = db.relationship('Event')
    message_log = db.relationship('MessageLog')
    
    def __repr__(self):
        return f'<ScheduledMessage {self.id} scheduled={self.scheduled_at} status={self.status}>'
