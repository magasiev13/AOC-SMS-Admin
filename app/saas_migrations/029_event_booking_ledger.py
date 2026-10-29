"""Add independent event bookings; retain old contact rows as legacy evidence."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    if 'event_registrations' not in inspect(connection).get_table_names():
        raise RuntimeError('Booking migration requires the existing event registration schema.')
    from app.models import EventBooking
    table = EventBooking.__table__
    table.create(connection, checkfirst=True)
    for index in table.indexes:
        index.create(connection, checkfirst=True)
    connection.execute(text("""
        INSERT INTO event_bookings
          (organization_id,event_id,source,source_scope,external_booking_id,status,is_active,party_size,name,phone,
           contact_state,selections_json,comment,external_person_id,source_updated_at,last_received_at,
           projection_registration_id,legacy_evidence,created_at)
        SELECT r.organization_id,r.event_id,COALESCE(r.external_source,'legacy'),'',
          COALESCE(r.external_booking_id,'legacy:' || CAST(r.id AS VARCHAR)),
          COALESCE(r.external_booking_status,'legacy'),TRUE,
          CASE WHEN COALESCE(r.booking_spaces,1) < 0 THEN 0 ELSE COALESCE(r.booking_spaces,1) END,r.name,r.phone,
          'valid',r.selections_json,r.booking_comment,r.external_person_id,r.external_updated_at,
          COALESCE(r.synced_at,r.created_at,CURRENT_TIMESTAMP),r.id,TRUE,COALESCE(r.created_at,CURRENT_TIMESTAMP)
        FROM event_registrations r
        WHERE NOT EXISTS (SELECT 1 FROM event_bookings b WHERE b.projection_registration_id=r.id)
    """))
    logger.info('Booking ledger added. Existing registration IDs remain intact; legacy rows do not establish complete roster coverage.')
