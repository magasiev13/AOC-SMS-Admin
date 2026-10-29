"""Add complete-inventory evidence; source authority remains explicitly disabled."""

def apply(connection, logger):
    from app.booking_inventory_models import BookingInventory, BookingInventoryPage, EventRegistrationSource
    for model in (EventRegistrationSource, BookingInventory, BookingInventoryPage):
        model.__table__.create(connection, checkfirst=True)
        for index in model.__table__.indexes:
            index.create(connection, checkfirst=True)
    logger.info('Booking inventory evidence added; no historical roster or authority was inferred.')
