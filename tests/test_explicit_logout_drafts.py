"""Deliberate sign-out clears only that user's browser draft namespace."""
from tests.test_ux_recovery import ux, login


def test_explicit_logout_marks_user_cleanup_and_login_emits_it_once(ux):
    login(ux)
    user_id = ux.users['owner']
    response = ux.client.post('/logout')
    assert response.status_code == 302 and response.location.endswith('/login')
    with ux.client.session_transaction() as session:
        assert session['clear_send_drafts_for_user_id'] == user_id
        assert '_user_id' not in session

    response = ux.client.get('/login')
    assert response.status_code == 200
    assert f'const prefix = "twinevia:send-draft:{user_id}:";'.encode() in response.data
    assert b'key.startsWith(prefix)' in response.data
    with ux.client.session_transaction() as session:
        assert 'clear_send_drafts_for_user_id' not in session
    assert b'key.startsWith(prefix)' not in ux.client.get('/login').data


def test_automatic_logout_does_not_mark_draft_deletion(ux):
    from app.auth import logout_and_clear_session

    with ux.app.test_request_context('/dashboard'):
        from flask import session
        logout_and_clear_session()
        assert 'clear_send_drafts_for_user_id' not in session

    response = ux.client.post('/dashboard/review', headers={'Accept': 'application/json'})
    assert response.status_code == 401
    assert b'key.startsWith(prefix)' not in ux.client.get('/login').data
