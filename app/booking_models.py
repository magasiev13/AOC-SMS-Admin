"""Source booking identities, retained separately from SMS destinations."""
import json

from app import db
from app.models import utc_now


class EventBooking(db.Model):
    __tablename__ = 'event_bookings'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    source = db.Column(db.String(80), nullable=False)
    source_scope = db.Column(db.String(160), nullable=False, default='')
    external_booking_id = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='active')
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    party_size = db.Column(db.Integer, nullable=False, default=1)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=True, index=True)
    contact_state = db.Column(db.String(16), nullable=False, default='missing')
    sms_consent = db.Column(db.Boolean, nullable=True)
    selections_json = db.Column(db.Text, nullable=True)
    comment = db.Column(db.Text, nullable=True)
    external_person_id = db.Column(db.String(80), nullable=True)
    source_revision = db.Column(db.BigInteger, nullable=True)
    source_updated_at = db.Column(db.DateTime, nullable=True)
    payload_fingerprint = db.Column(db.String(64), nullable=True)
    last_received_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancellation_history_json = db.Column(db.Text, nullable=True)
    projection_registration_id = db.Column(db.Integer, db.ForeignKey('event_registrations.id', ondelete='SET NULL'), nullable=True, index=True)
    duplicate_of_id = db.Column(db.Integer, db.ForeignKey('event_bookings.id'), nullable=True, index=True)
    legacy_evidence = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    event = db.relationship('Event')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'source', 'source_scope', 'external_booking_id', name='ux_event_bookings_source_identity'),
        db.CheckConstraint('party_size >= 0', name='ck_event_bookings_party_size'),
    )

    @property
    def cancellation_history(self):
        try:
            value = json.loads(self.cancellation_history_json or '[]')
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    @property
    def capacity_spaces(self):
        return self.party_size

    @property
    def selection_summary(self):
        try:
            selections = json.loads(self.selections_json or '[]')
        except (TypeError, ValueError):
            return ''
        return '; '.join(f"{item.get('label', '')} ({item.get('quantity', 0)})" for item in selections if isinstance(item, dict))

    def __repr__(self):
        return f'<EventBooking id={self.id} event={self.event_id} status={self.status}>'
