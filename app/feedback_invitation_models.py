"""Persistent, explicitly approved authority for one event feedback invitation."""
from app import db
from app.models import utc_now


class FeedbackInvitation(db.Model):
    __tablename__ = 'feedback_invitations'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey_flows.id'), nullable=False, index=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    approved_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    approval_key = db.Column(db.String(64), nullable=False)
    approval_digest = db.Column(db.String(64), nullable=False)
    approval_history_json = db.Column(db.Text, nullable=False, default='[]')
    context_digest = db.Column(db.String(64), nullable=False)
    message_digest = db.Column(db.String(64), nullable=False)
    form_version = db.Column(db.String(64), nullable=False)
    audience_rule = db.Column(db.String(24), nullable=False)
    timezone = db.Column(db.String(80), nullable=False)
    scheduled_message_id = db.Column(db.Integer, db.ForeignKey('scheduled_messages.id'), nullable=False, unique=True)
    status = db.Column(db.String(24), nullable=False, default='approved', index=True)
    hold_reason = db.Column(db.Text, nullable=True)
    snapshot_frozen_at = db.Column(db.DateTime, nullable=True)
    recipient_count = db.Column(db.Integer, nullable=True)
    snapshot_digest = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    event = db.relationship('Event')
    survey = db.relationship('SurveyFlow')
    scheduled_message = db.relationship('ScheduledMessage')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'author_user_id', 'approval_key', name='ux_feedback_invitation_actor_approval'),
        db.CheckConstraint("audience_rule IN ('registered_contacts', 'community')", name='ck_feedback_invitation_audience'),
    )
