"""Add explicit feedback invitation authority without creating any schedules."""
from app.feedback_invitation_models import FeedbackInvitation


def apply(connection, logger):
    FeedbackInvitation.__table__.create(bind=connection, checkfirst=True)
    logger.info('Feedback invitation ledger created. No event, feedback form, or existing schedule was changed.')
