"""Workspace account recovery, verified mail and personal reply notices."""
import hashlib

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from redis.exceptions import RedisError

from app import db
from app.models import AppUser
from app.queue import get_redis_connection
from app.services.account_mail_service import lock_link, mail_ready, request_account_link
from app.services.auth_security_service import (
    check_login_limited, clear_account_failed_logins, is_password_reused, password_policy_errors,
    record_auth_event, record_failed_login, store_password_history,
)

bp = Blueprint('account_mail', __name__)


def _allow_request():
    # Atomically rate-limit per caller without accumulating or exposing email addresses.
    address = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
    try:
        return bool(get_redis_connection().set(f'account-mail:request:{address}', '1', nx=True, ex=10))
    except RedisError:
        current_app.logger.warning('Account recovery rate limiter unavailable')
        return False


@bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    ready = mail_ready()
    if request.method == 'POST' and ready:
        email = request.form.get('email', '').strip().lower()
        if len(email) <= 255 and _allow_request():
            user = AppUser.query.filter(db.func.lower(AppUser.email) == email).first()
            if user:
                request_account_link(user.id, 'recover')
        flash('If this workspace account has a verified recovery email, a single-use link will arrive shortly.', 'info')
        return redirect(url_for('account_mail.forgot_password'))
    return render_template('auth/account_link.html', mode='request', ready=ready)


@bp.route('/account/email/<any(verify,recover):kind>', methods=['GET', 'POST'])
def account_link(kind):
    if request.method == 'POST':
        row, user = lock_link(request.form.get('code', '').strip(), kind)
        if not row or (current_user.is_authenticated and user.id != current_user.id):
            flash('This link has expired or is no longer valid.', 'error')
        elif kind == 'verify':
            user.verified_recovery_email = user.email
            user.primary_membership.reply_notice_email = None
            row.status = 'used'
            db.session.commit()
            record_auth_event('recovery_email_verified', user=user)
            flash('Email verified. You can now recover your account and turn on reply notices.', 'success')
            return redirect(url_for('account_mail.preferences' if current_user.is_authenticated else 'auth.login'))
        else:
            password = request.form.get('password', '')
            history = current_app.config.get('PASSWORD_HISTORY_COUNT', 3)
            errors = password_policy_errors(password, username=user.username)
            if not password or len(password) > 1024 or password != request.form.get('confirmation'):
                errors.append('Enter a matching password and confirmation of at most 1024 characters.')
            if not errors and is_password_reused(user, password, history):
                errors.append('Choose a password different from recently used passwords.')
            if not errors:
                previous = user.password_hash
                user.set_password(password)
                user.rotate_session_nonce()
                user.must_change_password = False
                row.status = 'used'
                store_password_history(user.id, previous, history)
                db.session.commit()
                for identifier in {user.username, user.email}:
                    clear_account_failed_logins(identifier)
                record_auth_event('password_recovered', user=user)
                flash('Password updated. Sign in with your new password.', 'success')
                return redirect(url_for('auth.login'))
            for message in errors:
                flash(message, 'error')
    response = current_app.make_response(render_template('auth/account_link.html', mode=kind, ready=mail_ready()))
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    return response


def _confirm_current_password():
    ip = request.remote_addr or 'unknown'
    username = current_user.username
    limited, _, _ = check_login_limited(ip, username)
    if limited:
        return False
    if current_user.check_password(request.form.get('password', '')):
        return True
    record_failed_login(ip, username)
    return False


@bp.route('/account/email', methods=['GET', 'POST'])
@login_required
def preferences():
    if not current_app.config['SAAS_MODE'] or current_user.is_platform_admin or not current_user.primary_membership:
        abort(403)
    member = current_user.primary_membership
    ready = mail_ready()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'disable':
            member.reply_notice_email = None
            db.session.commit()
            flash('Email reply notices are off.', 'success')
        elif not ready:
            flash('Email delivery is unavailable. Contact your workspace administrator.', 'error')
        elif action == 'verify' and current_user.email and _confirm_current_password():
            request_account_link(current_user.id, 'verify')
            flash('Check your account email for a verification link.', 'info')
        elif action == 'enable' and current_user.email and current_user.email == current_user.verified_recovery_email:
            member.reply_notice_email = current_user.email
            db.session.commit()
            flash('Email reply notices are on for this workspace.', 'success')
        else:
            flash('Verify your account email with your current password.', 'error')
        return redirect(url_for('account_mail.preferences'))
    return render_template('auth/account_mail_preferences.html', ready=ready, member=member)
