"""Message work leads the header; history maintenance remains available below."""
from tests.test_ux_recovery import ux, login


def _page_header(response):
    return response.get_data(as_text=True).split('<header class="app-page-header">', 1)[1].split('</header>', 1)[0]


def test_history_header_composes_and_clear_action_stays_in_owner_maintenance(ux):
    ux.db.session.add(ux.models.MessageLog(organization_id=ux.org_id, message_body='Retained history fixture',
        target='community', status='sent', total_recipients=1, success_count=1, failure_count=0, details='[]'))
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/logs')
    assert response.status_code == 200
    header = _page_header(response)
    assert 'Compose Message' in header and 'href="/dashboard"' in header
    assert 'btn-primary page-action-primary' in header and 'Clear Removable History' not in header
    maintenance = response.get_data(as_text=True).split('data-history-maintenance>', 1)[1].split('</details>', 1)[0]
    assert 'History maintenance' in maintenance and 'Clear Removable History' in maintenance
    assert 'data-bs-target="#clearLogsModal"' in maintenance
    assert b'action="/logs/clear"' in response.data
    login(ux, 'staff')
    staff_response = ux.client.get('/logs')
    assert b'Compose Message' in staff_response.data
    assert b'data-history-maintenance' not in staff_response.data and b'id="clearLogsModal"' not in staff_response.data


def test_inbox_surveys_remains_accessible_without_a_primary_filled_button(ux):
    login(ux)
    response = ux.client.get('/inbox')
    assert response.status_code == 200
    header = _page_header(response)
    assert 'Surveys' in header and 'Automatic Replies' in header
    assert 'btn-primary' not in header and 'page-action-primary' not in header
    assert 'btn-outline-secondary page-action-secondary' in header
