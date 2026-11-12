"""Block categories and observation sources use plain display labels."""
from tests.test_ux_recovery import ux, login


def test_opt_out_table_and_mobile_cards_use_plain_labels_and_preserve_facts(ux):
    owner = ux.db.session.get(ux.models.AppUser, ux.users['owner'])
    record = ux.models.SuppressedContact(organization_id=ux.org_id, phone=owner.phone,
        reason='Retained provider evidence', category='hard_fail', source='usage_reconciliation')
    ux.db.session.add(record)
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/unsubscribed')
    assert response.status_code == 200
    assert response.data.count(b'Number blocked</span>') == 2
    assert response.data.count(b'Provider status check</span>') == 2
    assert b'hard_fail' not in response.data and b'usage_reconciliation' not in response.data
    assert (record.category, record.source) == ('hard_fail', 'usage_reconciliation')


def test_unknown_block_labels_have_readable_fallback_and_keep_staff_privacy(ux):
    owner = ux.db.session.get(ux.models.AppUser, ux.users['owner'])
    ux.db.session.add(ux.models.SuppressedContact(organization_id=ux.org_id, phone=owner.phone,
        category='custom_block', source='future_source_check'))
    ux.db.session.commit()
    login(ux, 'staff')
    response = ux.client.get('/unsubscribed')
    assert response.status_code == 200
    assert b'Custom block' in response.data and b'Future source check' in response.data
    assert owner.phone.encode() not in response.data
