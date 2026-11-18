"""Retain the exact historical signup contracts visible at upgrade time."""
import os

from sqlalchemy import select

from app.models import ConsentSource, Organization, utc_now


def apply(connection, logger):
    sources = ConsentSource.__table__
    sources.create(bind=connection, checkfirst=True)
    default = os.environ.get('AOC_EVENTS_ORGANIZATION_SLUG', 'armenians-of-colorado')
    configured = os.environ.get('COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS', default)
    slugs = [slug.strip() for slug in configured.split(',') if slug.strip()]
    organizations = Organization.__table__
    ids = connection.scalars(select(organizations.c.id).where(organizations.c.slug.in_(slugs)))
    for organization_id in ids.all():
        prior = select(sources.c.id).where(sources.c.organization_id == organization_id,
                                           sources.c.kind == 'historical')
        if connection.scalar(prior) is None:
            connection.execute(sources.insert().values(
                organization_id=organization_id, kind='historical', name='Existing signup contract',
                disclosure='Existing organization signup contract retained from deployment configuration.',
                purpose='community', status='approved', attested_at=utc_now(), reviewed_at=utc_now(),
            ))
