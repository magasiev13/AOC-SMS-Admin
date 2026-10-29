"""Tenant-scoped evidence for complete source inventories, never inferred from replays."""
from app import db
from app.models import utc_now


class EventRegistrationSource(db.Model):
    __tablename__ = 'event_registration_sources'
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    source = db.Column(db.String(80), nullable=False)
    source_scope = db.Column(db.String(160), nullable=False)
    authority_enabled = db.Column(db.Boolean, nullable=False, default=False)
    verified_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    verified_at = db.Column(db.DateTime, nullable=True)
    last_inventory_id = db.Column(db.String(100), nullable=True)
    pending_inventory_id = db.Column(db.String(100), nullable=True)
    pending_watermark = db.Column(db.DateTime, nullable=True)
    last_complete_at = db.Column(db.DateTime, nullable=True)
    last_watermark = db.Column(db.DateTime, nullable=True)
    coverage_status = db.Column(db.String(24), nullable=False, default='unverified')
    __table_args__ = (db.UniqueConstraint('organization_id', 'event_id', 'source', 'source_scope', name='uq_event_registration_source'),)


class BookingInventory(db.Model):
    __tablename__ = 'booking_inventories'
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    inventory_id = db.Column(db.String(100), nullable=False)
    source = db.Column(db.String(80), nullable=False)
    source_scope = db.Column(db.String(160), nullable=False)
    watermark = db.Column(db.DateTime, nullable=False)
    page_count = db.Column(db.Integer, nullable=False)
    record_count = db.Column(db.Integer, nullable=False)
    manifest_digest = db.Column(db.String(64), nullable=False)
    completion_proof = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(24), nullable=False, default='receiving')
    started_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    completed_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint('organization_id', 'inventory_id', name='uq_booking_inventory_identity'),)


class BookingInventoryPage(db.Model):
    __tablename__ = 'booking_inventory_pages'
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    inventory_id = db.Column(db.Integer, db.ForeignKey('booking_inventories.id'), nullable=False, index=True)
    page_number = db.Column(db.Integer, nullable=False)
    payload_digest = db.Column(db.String(64), nullable=False)
    records_json = db.Column(db.Text, nullable=False)
    __table_args__ = (db.UniqueConstraint('inventory_id', 'page_number', name='uq_booking_inventory_page'),)
