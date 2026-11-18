"""Set up a workspace with the same source record as a migrated signup contract."""


def historical_signup_contract(organization_id, enabled=True):
    from app import db
    from app.models import ConsentSource

    row = ConsentSource.query.filter_by(organization_id=organization_id, kind='historical').first()
    if row is None:
        row = ConsentSource(organization_id=organization_id, kind='historical', purpose='community',
                            name='Existing signup contract', disclosure='Fixture signup disclosure')
        db.session.add(row)
    row.status = 'approved' if enabled else 'withdrawn'
    db.session.flush()
