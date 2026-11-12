"""Billing facts use their real count without hidden or empty placeholder cells."""
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from tests.test_ux_recovery import ux, login


class Highlights(HTMLParser):
    def __init__(self):
        super().__init__()
        self.grids = []
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get('class', '').split()
        if 'workspace-summary__stats' in classes:
            self.grids.append(classes)
        if 'workspace-summary__stat' in classes:
            self.cells += 1


@pytest.mark.parametrize('status,count', [('complimentary', 5), ('active', 6)])
def test_actual_billing_states_render_the_real_five_or_six_fact_count(ux, status, count):
    organization = ux.db.session.get(ux.models.Organization, ux.org_id)
    organization.subscription.status = status
    organization.subscription.current_period_end = ux.models.utc_now() + timedelta(days=30)
    organization.subscription.activation_fee_paid_at = ux.models.utc_now()
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/billing')
    assert response.status_code == 200
    html = Highlights()
    html.feed(response.get_data(as_text=True))
    assert len(html.grids) == 1
    assert f'workspace-summary__stats--{count}' in html.grids[0]
    assert html.cells == count
