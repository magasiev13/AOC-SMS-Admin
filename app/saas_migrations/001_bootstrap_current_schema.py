from app import db
from app import models  # noqa: F401


def apply(connection, logger) -> None:
    db.metadata.create_all(bind=connection)
    logger.info("SaaS migration 001: created current SQLAlchemy metadata tables.")
