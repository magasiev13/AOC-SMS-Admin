import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, unquote
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import login_required, current_user
from sqlalchemy import DateTime, Integer, String, Text, func, text
from sqlalchemy.exc import OperationalError

from app import csrf, db
from app.auth import require_roles

from app.models import (
    AppUser,
    CommunityMember,
    Event,
    EventRegistration,
    InboxMessage,
    InboxThread,
    KeywordAutomationRule,
    MessageLog,
    ScheduledMessage,
    SuppressedContact,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.inbox_service import (
    delete_messages_in_thread,
    delete_thread_with_dependencies,
    mark_thread_read,
    parse_survey_questions,
    process_inbound_sms,
    send_thread_reply,
    update_thread_contact_name,
)
from app.services.recipient_service import (
    filter_suppressed_recipients,
    filter_unsubscribed_recipients,
    get_unsubscribed_phone_set,
)
from app.services.twilio_service import validate_inbound_signature
from app.sort_utils import normalize_sort_params
from app.utils import (
    ALLOWED_TEMPLATE_TOKENS,
    escape_like,
    find_invalid_template_tokens,
    normalize_keyword,
    normalize_phone,
    parse_recipients_csv,
    validate_phone,
)

bp = Blueprint('main', __name__)


def _is_safe_url(target):
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ('http', 'https') and host_url.netloc == redirect_url.netloc


def _normalized_keyword_sql(column):
    """
    Normalize keyword-like values in SQL to mirror normalize_keyword().

    This keeps conflict checks in the database and avoids loading all rows
    into Python when legacy rows might contain non-canonical whitespace.
    """
    normalized = func.upper(func.trim(column))
    for token in ('\t', '\n', '\r', '\f', '\v'):
        normalized = func.replace(normalized, token, ' ')
    for _ in range(6):
        normalized = func.replace(normalized, '  ', ' ')
    return normalized


def _keyword_conflicts_with_survey(trigger_keyword: str, *, exclude_survey_id: int | None = None) -> bool:
    normalized_trigger = normalize_keyword(trigger_keyword)
    if not normalized_trigger:
        return False

    query = SurveyFlow.query.filter(_normalized_keyword_sql(SurveyFlow.trigger_keyword) == normalized_trigger)
    if exclude_survey_id is not None:
        query = query.filter(SurveyFlow.id != exclude_survey_id)
    return query.first() is not None


def _keyword_conflicts_with_rule(keyword: str, *, exclude_rule_id: int | None = None) -> bool:
    normalized_keyword = normalize_keyword(keyword)
    if not normalized_keyword:
        return False

    query = KeywordAutomationRule.query.filter(
        _normalized_keyword_sql(KeywordAutomationRule.keyword) == normalized_keyword
    )
    if exclude_rule_id is not None:
        query = query.filter(KeywordAutomationRule.id != exclude_rule_id)
    return query.first() is not None


def _community_name_map_for_phones(phones: set[str]) -> dict[str, str]:
    if not phones:
        return {}

    members = CommunityMember.query.filter(CommunityMember.phone.in_(phones)).all()
    community_name_map = {}
    for member in members:
        name = (member.name or '').strip()
        if name:
            community_name_map[member.phone] = name
    return community_name_map


def _build_thread_display_names(
    threads: list[InboxThread],
    selected_thread: InboxThread | None = None,
) -> dict[int, str]:
    phones = {thread.phone for thread in threads if thread.phone}
    if selected_thread and selected_thread.phone:
        phones.add(selected_thread.phone)

    community_name_map = _community_name_map_for_phones(phones)
    display_names: dict[int, str] = {}

    for thread in threads:
        thread_name = (thread.contact_name or '').strip()
        display_names[thread.id] = community_name_map.get(thread.phone) or thread_name or thread.phone

    if selected_thread and selected_thread.id not in display_names:
        selected_name = (selected_thread.contact_name or '').strip()
        display_names[selected_thread.id] = (
            community_name_map.get(selected_thread.phone) or selected_name or selected_thread.phone
        )

    return display_names


def _parse_int_ids(raw_values: list[str]) -> list[int]:
    ids: list[int] = []
    for raw in raw_values:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))


def _redirect_to_inbox(*, thread_id: int | None = None) -> object:
    search = request.form.get('search', '').strip()
    query_args = {}
    if thread_id:
        query_args['thread'] = thread_id
    if search:
        query_args['search'] = search
    return redirect(url_for('main.inbox_list', **query_args))


def _phone_digits_sql(column):
    """Normalize stored phone values to digits-only for flexible search matching."""
    normalized = func.replace(column, '+', '')
    for token in ('(', ')', '-', ' ', '.'):
        normalized = func.replace(normalized, token, '')
    return normalized


def _build_survey_submission_data(survey: SurveyFlow, *, search: str = '') -> dict[str, object]:
    questions = survey.questions
    completed_sessions = (
        SurveySession.query.filter_by(survey_id=survey.id, status='completed')
        .order_by(SurveySession.completed_at.desc(), SurveySession.id.desc())
        .all()
    )
    if not completed_sessions:
        return {
            'questions': questions,
            'all_completed_rows': [],
            'latest_rows': [],
            'history_by_phone': {},
            'unique_attendees': 0,
            'total_completed': 0,
            'repeat_submitters': 0,
        }

    session_ids = [session.id for session in completed_sessions]
    responses = (
        SurveyResponse.query.filter(SurveyResponse.session_id.in_(session_ids))
        .order_by(SurveyResponse.session_id.asc(), SurveyResponse.question_index.asc(), SurveyResponse.id.asc())
        .all()
    )

    answers_by_session: dict[int, dict[int, str]] = {}
    for response in responses:
        answer_map = answers_by_session.setdefault(response.session_id, {})
        if response.question_index not in answer_map:
            answer_map[response.question_index] = (response.answer or '').strip()

    phones = {session.phone for session in completed_sessions if session.phone}
    community_name_map = _community_name_map_for_phones(phones)
    thread_name_map = {
        thread.phone: (thread.contact_name or '').strip()
        for thread in InboxThread.query.filter(InboxThread.phone.in_(phones)).all()
        if (thread.contact_name or '').strip()
    }

    all_completed_rows: list[dict[str, object]] = []
    submission_counts: dict[str, int] = {}
    for session in completed_sessions:
        submission_counts[session.phone] = submission_counts.get(session.phone, 0) + 1
        answer_map = answers_by_session.get(session.id, {})
        answers = [answer_map.get(index, '') for index in range(len(questions))]
        first_answer = next((answer for answer in answers if answer), '')
        display_name = (
            community_name_map.get(session.phone)
            or thread_name_map.get(session.phone)
            or first_answer
            or session.phone
        )
        answer_preview_items = [answer for answer in answers if answer][:2]
        answers_preview = '; '.join(answer_preview_items)
        if len(answers_preview) > 120:
            answers_preview = f"{answers_preview[:117].rstrip()}..."

        submitted_at = session.completed_at or session.last_activity_at or session.started_at
        submitted_at_iso = submitted_at.isoformat() if submitted_at else ''

        all_completed_rows.append(
            {
                'session_id': session.id,
                'phone': session.phone,
                'display_name': display_name,
                'submitted_at': submitted_at,
                'submitted_at_iso': submitted_at_iso,
                'answers': answers,
                'qa_pairs': [
                    {'prompt': question, 'answer': answers[index] if index < len(answers) else ''}
                    for index, question in enumerate(questions)
                ],
                'answers_preview': answers_preview,
                'thread_id': session.thread_id,
            }
        )

    latest_rows: list[dict[str, object]] = []
    history_by_phone: dict[str, list[dict[str, object]]] = {}
    latest_session_by_phone: dict[str, int] = {}

    for row in all_completed_rows:
        phone = str(row['phone'])
        if phone not in latest_session_by_phone:
            latest_session_by_phone[phone] = int(row['session_id'])
            row['submission_count'] = submission_counts.get(phone, 0)
            row['phone_dom_id'] = re.sub(r'[^0-9]', '', phone) or f"phone{row['session_id']}"
            latest_rows.append(row)
            continue
        history_by_phone.setdefault(phone, []).append(row)

    search_text = search.strip().casefold()
    if search_text:
        filtered_latest_rows: list[dict[str, object]] = []
        filtered_history_by_phone: dict[str, list[dict[str, object]]] = {}
        for row in latest_rows:
            phone = str(row['phone'])
            history_rows = history_by_phone.get(phone, [])
            haystack: list[str] = [
                phone,
                str(row['display_name']),
                str(row['answers_preview']),
            ]
            haystack.extend(str(answer) for answer in row.get('answers', []) if answer)
            for history_row in history_rows:
                haystack.append(str(history_row['display_name']))
                haystack.append(str(history_row['answers_preview']))
                haystack.extend(str(answer) for answer in history_row.get('answers', []) if answer)
            if any(search_text in value.casefold() for value in haystack if value):
                filtered_latest_rows.append(row)
                filtered_history_by_phone[phone] = history_rows
        latest_rows = filtered_latest_rows
        history_by_phone = filtered_history_by_phone

    total_completed = sum(int(row.get('submission_count') or 0) for row in latest_rows)
    repeat_submitters = sum(1 for row in latest_rows if int(row.get('submission_count') or 0) > 1)

    return {
        'questions': questions,
        'all_completed_rows': all_completed_rows,
        'latest_rows': latest_rows,
        'history_by_phone': history_by_phone,
        'latest_session_by_phone': latest_session_by_phone,
        'unique_attendees': len(latest_rows),
        'total_completed': total_completed,
        'repeat_submitters': repeat_submitters,
    }


def _survey_form_events() -> list[Event]:
    return Event.query.order_by(Event.date.desc(), Event.title.asc()).all()


def _render_survey_form(*, survey: SurveyFlow | None, form_data: dict | None):
    return render_template(
        'inbox/survey_form.html',
        survey=survey,
        form_data=form_data,
        events=_survey_form_events(),
    )


# Health check endpoint
@bp.route('/health')
def health():
    return 'OK', 200


@bp.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.svg'), code=302)


# Redirect root to dashboard
@bp.route('/')
@login_required
def index():
    return redirect(url_for('main.dashboard'))


# Dashboard - Send Messages
@bp.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    from flask import current_app
    app_timezone = current_app.config.get('APP_TIMEZONE', 'UTC')
    client_timezone_raw = request.cookies.get('client_timezone', '')
    client_timezone = unquote(client_timezone_raw).strip() if client_timezone_raw else ''
    dashboard_timezone = client_timezone or app_timezone
    events = Event.query.order_by(Event.date.desc()).all()
    admin_test_phone = current_app.config.get('ADMIN_TEST_PHONE')

    def build_chart_data():
        """Build 7-day delivery trends data for the dashboard chart."""
        tz = None
        try:
            tz = ZoneInfo(dashboard_timezone)
        except Exception:
            if dashboard_timezone != app_timezone:
                try:
                    tz = ZoneInfo(app_timezone)
                except Exception:
                    tz = timezone.utc
            else:
                tz = timezone.utc

        today = datetime.now(tz).date()
        labels = []
        sent_data = []
        failed_data = []
        
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            labels.append(day.strftime('%b %d'))
            
            day_start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            day_end_local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            day_start = day_start_local.astimezone(timezone.utc).replace(tzinfo=None)
            day_end = day_end_local.astimezone(timezone.utc).replace(tzinfo=None)
            
            try:
                logs = MessageLog.query.filter(
                    MessageLog.created_at >= day_start,
                    MessageLog.created_at < day_end
                ).all()
                
                day_sent = sum(log.success_count or 0 for log in logs)
                day_failed = sum(log.failure_count or 0 for log in logs)
            except OperationalError:
                day_sent = 0
                day_failed = 0
            
            sent_data.append(day_sent)
            failed_data.append(day_failed)
        
        if any(sent_data) or any(failed_data):
            return {
                'trends': {
                    'labels': labels,
                    'sent': sent_data,
                    'failed': failed_data
                }
            }
        return None

    def build_dashboard_context():
        community_count = CommunityMember.query.count()
        event_registration_count = EventRegistration.query.count()
        total_recipients = community_count + event_registration_count
        unsubscribed_count = UnsubscribedContact.query.count()
        inbound_count_7d = 0
        unread_threads_count = 0
        active_survey_sessions = 0
        top_keywords = []
        latest_log = None
        recent_logs = []
        seven_days_ago = utc_now().replace(tzinfo=None) - timedelta(days=7)
        try:
            latest_log = MessageLog.query.order_by(MessageLog.created_at.desc()).first()
            recent_logs = MessageLog.query.order_by(MessageLog.created_at.desc()).limit(5).all()
            inbound_count_7d = InboxMessage.query.filter(
                InboxMessage.direction == 'inbound',
                InboxMessage.created_at >= seven_days_ago,
            ).count()
            unread_threads_count = InboxThread.query.filter(InboxThread.unread_count > 0).count()
            active_survey_sessions = SurveySession.query.filter_by(status='active').count()
            keyword_rows = db.session.query(
                InboxMessage.matched_keyword,
                db.func.count(InboxMessage.id).label('hits'),
            ).filter(
                InboxMessage.direction == 'inbound',
                InboxMessage.created_at >= seven_days_ago,
                InboxMessage.matched_keyword.isnot(None),
            ).group_by(
                InboxMessage.matched_keyword,
            ).order_by(
                db.func.count(InboxMessage.id).desc(),
            ).limit(5).all()
            top_keywords = [
                {'keyword': row[0], 'hits': row[1]}
                for row in keyword_rows
            ]
        except OperationalError as exc:
            db.session.rollback()
            current_app.logger.warning(
                'Dashboard query failed due to schema mismatch: %s',
                exc,
            )
        pending_scheduled_count = ScheduledMessage.query.filter_by(status='pending').count()
        success_rate = None
        failure_rate = None
        if latest_log and latest_log.total_recipients:
            success_rate = round((latest_log.success_count / latest_log.total_recipients) * 100, 1)
            failure_rate = round((latest_log.failure_count / latest_log.total_recipients) * 100, 1)

        chart_data = build_chart_data()

        return {
            'community_count': community_count,
            'event_registration_count': event_registration_count,
            'total_recipients': total_recipients,
            'unsubscribed_count': unsubscribed_count,
            'latest_log': latest_log,
            'recent_logs': recent_logs,
            'pending_scheduled_count': pending_scheduled_count,
            'success_rate': success_rate,
            'failure_rate': failure_rate,
            'chart_data': chart_data,
            'inbound_count_7d': inbound_count_7d,
            'unread_threads_count': unread_threads_count,
            'active_survey_sessions': active_survey_sessions,
            'top_keywords': top_keywords,
        }

    def render_dashboard():
        return render_template(
            'dashboard.html',
            events=events,
            admin_test_phone=admin_test_phone,
            app_timezone=app_timezone,
            **build_dashboard_context()
        )
    
    if request.method == 'POST':
        message_body = request.form.get('message_body', '').strip()
        target = request.form.get('target', 'community')
        event_id = request.form.get('event_id', type=int)
        test_mode = request.form.get('test_mode') == 'on'
        include_unsubscribe = request.form.get('include_unsubscribe') == 'on'
        schedule_later = request.form.get('schedule_later') == 'on'
        schedule_date = request.form.get('schedule_date', '').strip()
        schedule_time = request.form.get('schedule_time', '').strip()
        client_timezone = request.form.get('client_timezone', '').strip()
        
        if not message_body:
            flash('Message body is required.', 'error')
            return render_dashboard()

        invalid_tokens = find_invalid_template_tokens(message_body)
        if invalid_tokens:
            allowed_tokens = ', '.join(f'{{{token}}}' for token in ALLOWED_TEMPLATE_TOKENS)
            invalid_list = ', '.join(invalid_tokens)
            flash(
                f'Invalid personalization token(s): {invalid_list}. Use {allowed_tokens}.',
                'error',
            )
            return render_dashboard()
        
        if target == 'event' and not event_id:
            flash('Please select an event.', 'error')
            return render_dashboard()
        
        # Handle scheduled message
        if schedule_later:
            if not schedule_date or not schedule_time:
                flash('Schedule date and time are required.', 'error')
                return render_dashboard()
            
            try:
                tz_name = client_timezone or app_timezone
                tz = None
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    if tz_name != app_timezone:
                        try:
                            tz = ZoneInfo(app_timezone)
                        except Exception:
                            tz = None
                if tz is None:
                    tz = timezone.utc

                scheduled_local = datetime.strptime(f'{schedule_date} {schedule_time}', '%Y-%m-%d %H:%M').replace(tzinfo=tz)
                scheduled_utc = scheduled_local.astimezone(timezone.utc).replace(tzinfo=None)

                if scheduled_utc <= datetime.utcnow():
                    flash('Scheduled time must be in the future.', 'error')
                    return render_dashboard()
                
                # Append unsubscribe text if option is checked
                final_message = message_body
                if include_unsubscribe:
                    final_message = message_body + "\n\nReply STOP to unsubscribe."
                
                scheduled = ScheduledMessage(
                    message_body=final_message,
                    target=target,
                    event_id=event_id if target == 'event' else None,
                    scheduled_at=scheduled_utc,
                    test_mode=test_mode
                )
                db.session.add(scheduled)
                db.session.commit()
                
                flash(f'Message scheduled for {scheduled_local.strftime("%Y-%m-%d %H:%M")}.', 'success')
                return redirect(url_for('main.scheduled_list'))
                
            except ValueError as e:
                flash(f'Invalid date/time format: {e}', 'error')
                return render_dashboard()
        
        # Immediate send
        # Test mode: send only to admin phone
        if test_mode:
            if not admin_test_phone:
                flash('ADMIN_TEST_PHONE not configured. Add it to your .env file.', 'error')
                return render_dashboard()
            recipient_data = [{'phone': admin_test_phone, 'name': 'Admin Test'}]
        else:
            # Get recipients based on target
            if target == 'community':
                members = CommunityMember.query.all()
                recipient_data = [{'phone': m.phone, 'name': m.name} for m in members]
            else:
                # Get registrations for the event (they store phone/name directly)
                registrations = EventRegistration.query.filter_by(event_id=event_id).all()
                recipient_data = [{'phone': r.phone, 'name': r.name} for r in registrations]

            recipient_data, skipped, _ = filter_unsubscribed_recipients(recipient_data)
            if skipped:
                flash(f'Skipped {len(skipped)} unsubscribed recipient(s).', 'warning')

            recipient_data, suppressed_skipped, _ = filter_suppressed_recipients(recipient_data)
            if suppressed_skipped:
                flash(f'Skipped {len(suppressed_skipped)} suppressed recipient(s).', 'warning')
        
        if not recipient_data:
            if test_mode:
                flash('No recipients found for the selected target.', 'error')
            else:
                flash('All recipients are unsubscribed or no recipients were found.', 'error')
            return render_dashboard()
        
        # Append unsubscribe text if option is checked
        final_message = message_body
        if include_unsubscribe:
            final_message = message_body + "\n\nReply STOP to unsubscribe."

        # Persist log before sending begins
        log = MessageLog(
            message_body=final_message,
            target=target,
            event_id=event_id if target == 'event' else None,
            status='processing',
            total_recipients=len(recipient_data),
            success_count=0,
            failure_count=0,
            details='[]'
        )
        db.session.add(log)
        db.session.commit()

        try:
            from rq import Retry
            from app.queue import get_queue

            queue = get_queue()
            queue.enqueue(
                'app.tasks.send_bulk_job',
                log.id,
                recipient_data,
                final_message,
                retry=Retry(max=3, interval=[30, 120, 300])
            )
            flash('Blast queued. Sending in the background.', 'success')
            return redirect(url_for('main.log_detail', log_id=log.id))
        except Exception as e:
            log.status = 'failed'
            log.details = json.dumps([{'error': str(e)}])
            db.session.commit()
            flash(f'Error queueing messages: {str(e)}', 'error')
    
    return render_dashboard()


# User Management
@bp.route('/users')
@login_required
@require_roles('admin')
def users_list():
    users = AppUser.query.order_by(AppUser.username).all()
    return render_template('users/list.html', users=users)


@bp.route('/users/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def users_add():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        role = request.form.get('role', '').strip()
        password = request.form.get('password', '')
        must_change_password = request.form.get('must_change_password') == 'on'

        if not username:
            flash('Username is required.', 'error')
            return render_template('users/form.html', user=None)

        if role not in {'admin', 'social_manager'}:
            flash('Role selection is required.', 'error')
            return render_template('users/form.html', user=None)

        if not password:
            flash('Password is required.', 'error')
            return render_template('users/form.html', user=None)

        existing = AppUser.query.filter_by(username=username).first()
        if existing:
            flash('A user with this username already exists.', 'error')
            return render_template('users/form.html', user=None)

        user = AppUser(username=username, role=role, must_change_password=must_change_password)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('User created successfully.', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('users/form.html', user=None)


@bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def users_edit(user_id):
    user = db.get_or_404(AppUser, user_id)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        role = request.form.get('role', '').strip()
        password = request.form.get('password', '')
        must_change_password = request.form.get('must_change_password') == 'on'

        if not username:
            flash('Username is required.', 'error')
            return render_template('users/form.html', user=user)

        if role not in {'admin', 'social_manager'}:
            flash('Role selection is required.', 'error')
            return render_template('users/form.html', user=user)

        existing = AppUser.query.filter(AppUser.username == username, AppUser.id != user_id).first()
        if existing:
            flash('A user with this username already exists.', 'error')
            return render_template('users/form.html', user=user)

        if user.role == 'admin' and role != 'admin':
            admin_count = AppUser.query.filter_by(role='admin').count()
            if admin_count <= 1:
                flash('At least one admin user is required.', 'error')
                return render_template('users/form.html', user=user)

        user.username = username
        user.role = role
        user.must_change_password = must_change_password
        if password:
            user.set_password(password)

        db.session.commit()
        flash('User updated successfully.', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('users/form.html', user=user)


@bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def users_delete(user_id):
    user = db.get_or_404(AppUser, user_id)

    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('main.users_list'))

    if user.role == 'admin':
        admin_count = AppUser.query.filter_by(role='admin').count()
        if admin_count <= 1:
            flash('At least one admin user is required.', 'error')
            return redirect(url_for('main.users_list'))

    db.session.delete(user)
    db.session.commit()
    flash('User deleted successfully.', 'success')
    return redirect(url_for('main.users_list'))


@bp.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not current_password:
            flash('Current password is required.', 'error')
            return render_template('auth/change_password.html')

        if not new_password:
            flash('New password is required.', 'error')
            return render_template('auth/change_password.html')

        if new_password != confirm_password:
            flash('New password and confirmation do not match.', 'error')
            return render_template('auth/change_password.html')

        if not current_user.check_password(current_password):
            flash('Current password is incorrect.', 'error')
            return render_template('auth/change_password.html')

        current_user.set_password(new_password)
        current_user.must_change_password = False
        db.session.commit()

        flash('Password updated successfully.', 'success')
        return redirect(url_for('main.dashboard'))

    return render_template('auth/change_password.html')


# Community Members Management
@bp.route('/community')
@login_required
def community_list():
    search = request.args.get('search', '').strip()
    
    query = CommunityMember.query
    
    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        search_filters = [
            CommunityMember.name.ilike(pattern, escape='\\'),
            CommunityMember.phone.ilike(pattern, escape='\\'),
        ]
        normalized_search_phone = normalize_phone(search)
        if validate_phone(normalized_search_phone):
            search_filters.append(CommunityMember.phone == normalized_search_phone)
        search_digits = re.sub(r'\D', '', search)
        if search_digits:
            digits_pattern = f'%{escape_like(search_digits)}%'
            search_filters.append(_phone_digits_sql(CommunityMember.phone).ilike(digits_pattern, escape='\\'))
        query = query.filter(
            db.or_(*search_filters)
        )
    
    members = query.order_by(CommunityMember.name, CommunityMember.phone).all()
    unsubscribed_phones = get_unsubscribed_phone_set([member.phone for member in members])
    return render_template('community/list.html', members=members, search=search, unsubscribed_phones=unsubscribed_phones)


@bp.route('/community/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        
        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('community/form.html', member=None)
        
        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('community/form.html', member=None)

        if UnsubscribedContact.query.filter_by(phone=phone).first():
            flash('This number is currently unsubscribed and will not receive messages.', 'warning')
        
        # Check for duplicate
        existing = CommunityMember.query.filter_by(phone=phone).first()
        if existing:
            flash('A member with this phone number already exists.', 'error')
            return render_template('community/form.html', member=None)
        
        member = CommunityMember(name=name, phone=phone)
        db.session.add(member)
        db.session.commit()
        
        flash('Community member added successfully.', 'success')
        return redirect(url_for('main.community_list'))
    
    return render_template('community/form.html', member=None)


@bp.route('/community/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_edit(member_id):
    member = db.get_or_404(CommunityMember, member_id)
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        
        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('community/form.html', member=member)
        
        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('community/form.html', member=member)

        if UnsubscribedContact.query.filter_by(phone=phone).first():
            flash('This number is currently unsubscribed and will not receive messages.', 'warning')
        
        # Check for duplicate (excluding current)
        existing = CommunityMember.query.filter(
            CommunityMember.phone == phone,
            CommunityMember.id != member_id
        ).first()
        if existing:
            flash('A member with this phone number already exists.', 'error')
            return render_template('community/form.html', member=member)
        
        member.name = name
        member.phone = phone
        db.session.commit()
        
        flash('Community member updated successfully.', 'success')
        return redirect(url_for('main.community_list'))
    
    return render_template('community/form.html', member=member)


@bp.route('/community/<int:member_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def community_delete(member_id):
    member = db.get_or_404(CommunityMember, member_id)
    db.session.delete(member)
    db.session.commit()
    flash('Community member deleted.', 'success')
    return redirect(url_for('main.community_list'))


@bp.route('/community/export')
@login_required
@require_roles('admin')
def community_export():
    members = CommunityMember.query.order_by(CommunityMember.name, CommunityMember.phone).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'phone', 'created_at'])
    for member in members:
        writer.writerow([member.name or '', member.phone, member.created_at.isoformat() if member.created_at else ''])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=community_members.csv'
    return response


@bp.route('/community/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def community_bulk_delete():
    raw_ids = request.form.getlist('member_ids')
    member_ids = []
    for raw in raw_ids:
        try:
            member_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    member_ids = sorted(set(member_ids))
    if not member_ids:
        flash('No members selected.', 'warning')
        return redirect(url_for('main.community_list'))

    deleted = CommunityMember.query.filter(CommunityMember.id.in_(member_ids)).delete(synchronize_session=False)
    db.session.commit()
    flash(f'Deleted {deleted} member(s).', 'success')
    return redirect(url_for('main.community_list'))


@bp.route('/community/import', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_import():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file uploaded.', 'error')
            return render_template('community/import.html')
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return render_template('community/import.html')
        
        try:
            content = file.read().decode('utf-8')
            parsed = parse_recipients_csv(content)
            
            if not parsed:
                flash('No valid members found in CSV.', 'error')
                return render_template('community/import.html')
            
            added = 0
            skipped = 0
            
            for rec in parsed:
                existing = CommunityMember.query.filter_by(phone=rec['phone']).first()
                if existing:
                    skipped += 1
                    continue
                
                member = CommunityMember(
                    name=rec['name'],
                    phone=rec['phone']
                )
                db.session.add(member)
                added += 1
            
            db.session.commit()
            flash(f'Imported {added} members. {skipped} duplicates skipped.', 'success')
            return redirect(url_for('main.community_list'))
            
        except Exception as e:
            flash(f'Error processing CSV: {str(e)}', 'error')
    
    return render_template('community/import.html')


@bp.route('/community/<int:member_id>/unsubscribe', methods=['POST'])
@login_required
@require_roles('admin')
def community_unsubscribe(member_id):
    member = db.get_or_404(CommunityMember, member_id)
    existing = UnsubscribedContact.query.filter_by(phone=member.phone).first()
    if existing:
        flash('That number is already unsubscribed.', 'warning')
        return redirect(url_for('main.community_list'))

    unsubscribe = UnsubscribedContact(
        name=member.name,
        phone=member.phone,
        source='community'
    )
    db.session.add(unsubscribe)
    db.session.commit()
    flash('Member added to unsubscribed list.', 'success')
    return redirect(url_for('main.community_list'))


# Events Management
@bp.route('/events')
@login_required
def events_list():
    search = request.args.get('search', '').strip()
    query = Event.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                Event.title.ilike(pattern, escape='\\'),
                db.cast(Event.date, db.String).ilike(pattern, escape='\\')
            )
        )

    events = query.order_by(Event.date.desc()).all()
    return render_template('events/list.html', events=events, search=search)


@bp.route('/events/add', methods=['GET', 'POST'])
@login_required
def event_add():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        date_str = request.form.get('date', '').strip()
        
        if not title:
            flash('Event title is required.', 'error')
            return render_template('events/form.html', event=None)
        
        from datetime import datetime
        event_date = None
        if date_str:
            try:
                event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format.', 'error')
                return render_template('events/form.html', event=None)
        
        event = Event(title=title, date=event_date)
        db.session.add(event)
        db.session.commit()
        
        flash('Event created successfully.', 'success')
        return redirect(url_for('main.event_detail', event_id=event.id))
    
    return render_template('events/form.html', event=None)


@bp.route('/events/<int:event_id>')
@login_required
def event_detail(event_id):
    event = db.get_or_404(Event, event_id)
    registrations = EventRegistration.query.filter_by(event_id=event_id).order_by(EventRegistration.name, EventRegistration.phone).all()
    unsubscribed_phones = get_unsubscribed_phone_set([reg.phone for reg in registrations])
    return render_template('events/detail.html', event=event, registrations=registrations, unsubscribed_phones=unsubscribed_phones)


@bp.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def event_edit(event_id):
    event = db.get_or_404(Event, event_id)
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        date_str = request.form.get('date', '').strip()
        
        if not title:
            flash('Event title is required.', 'error')
            return render_template('events/form.html', event=event)
        
        from datetime import datetime
        event_date = None
        if date_str:
            try:
                event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format.', 'error')
                return render_template('events/form.html', event=event)
        
        event.title = title
        event.date = event_date
        db.session.commit()
        
        flash('Event updated successfully.', 'success')
        return redirect(url_for('main.event_detail', event_id=event.id))
    
    return render_template('events/form.html', event=event)


@bp.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def event_delete(event_id):
    event = db.get_or_404(Event, event_id)
    db.session.delete(event)
    db.session.commit()
    flash('Event deleted.', 'success')
    return redirect(url_for('main.events_list'))


@bp.route('/events/<int:event_id>/register', methods=['POST'])
@login_required
def event_register(event_id):
    event = db.get_or_404(Event, event_id)
    name = request.form.get('name', '').strip() or None
    phone = request.form.get('phone', '').strip()
    
    if not phone:
        flash('Phone number is required.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    phone = normalize_phone(phone)
    if not validate_phone(phone):
        flash('Invalid phone number format.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))

    if UnsubscribedContact.query.filter_by(phone=phone).first():
        flash('This number is currently unsubscribed and will not receive messages.', 'warning')
    
    # Check if already registered for this event
    existing = EventRegistration.query.filter_by(event_id=event_id, phone=phone).first()
    if existing:
        flash('This phone number is already registered for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    registration = EventRegistration(event_id=event_id, name=name, phone=phone)
    db.session.add(registration)
    db.session.commit()
    
    flash('Registration added.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/unregister/<int:registration_id>', methods=['POST'])
@login_required
def event_unregister(event_id, registration_id):
    registration = EventRegistration.query.filter_by(id=registration_id, event_id=event_id).first()
    if not registration:
        flash('Registration not found for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    db.session.delete(registration)
    db.session.commit()
    flash('Registration removed.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/registrations/<int:registration_id>/unsubscribe', methods=['POST'])
@login_required
def event_registration_unsubscribe(event_id, registration_id):
    registration = EventRegistration.query.filter_by(id=registration_id, event_id=event_id).first()
    if not registration:
        flash('Registration not found for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))

    existing = UnsubscribedContact.query.filter_by(phone=registration.phone).first()
    if existing:
        flash('That number is already unsubscribed.', 'warning')
        return redirect(url_for('main.event_detail', event_id=event_id))

    unsubscribe = UnsubscribedContact(
        name=registration.name,
        phone=registration.phone,
        source=f'event:{event_id}'
    )
    db.session.add(unsubscribe)
    db.session.commit()
    flash('Registration added to unsubscribed list.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/import', methods=['POST'])
@login_required
def event_import_registrations(event_id):
    event = db.get_or_404(Event, event_id)
    
    if 'file' not in request.files:
        flash('No file uploaded.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    file = request.files['file']
    if file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    try:
        content = file.read().decode('utf-8')
        parsed = parse_recipients_csv(content)
        
        if not parsed:
            flash('No valid entries found in CSV.', 'error')
            return redirect(url_for('main.event_detail', event_id=event_id))
        
        added = 0
        already_registered = 0
        
        for rec in parsed:
            # Check if already registered for this event
            existing = EventRegistration.query.filter_by(event_id=event_id, phone=rec['phone']).first()
            if existing:
                already_registered += 1
                continue
            
            registration = EventRegistration(event_id=event_id, name=rec['name'], phone=rec['phone'])
            db.session.add(registration)
            added += 1
        
        db.session.commit()
        
        msg = f'Added {added} registrations.'
        if already_registered:
            msg += f' {already_registered} already registered.'
        
        flash(msg, 'success' if added > 0 else 'warning')
        
    except Exception as e:
        flash(f'Error processing CSV: {str(e)}', 'error')
    
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/export')
@login_required
def event_export_registrations(event_id):
    event = db.get_or_404(Event, event_id)
    registrations = EventRegistration.query.filter_by(event_id=event_id).order_by(EventRegistration.name, EventRegistration.phone).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'phone', 'created_at'])
    for reg in registrations:
        writer.writerow([reg.name or '', reg.phone, reg.created_at.isoformat() if reg.created_at else ''])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename=event_{event.id}_registrations.csv'
    return response


# Message Logs
@bp.route('/logs')
@login_required
def logs_list():
    search = request.args.get('search', '').strip()
    query = MessageLog.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.outerjoin(Event).filter(
            db.or_(
                MessageLog.message_body.ilike(pattern, escape='\\'),
                MessageLog.target.ilike(pattern, escape='\\'),
                Event.title.ilike(pattern, escape='\\')
            )
        )

    try:
        logs = query.order_by(MessageLog.created_at.desc()).limit(100).all()
    except OperationalError as exc:
        from flask import current_app
        current_app.logger.warning(
            'MessageLog list query failed due to schema mismatch: %s',
            exc,
        )
        flash('Logs are temporarily unavailable due to a schema mismatch.', 'error')
        logs = []

    processing_logs = [
        {
            'id': log.id,
            'status': log.status or 'sent',
            'success_count': log.success_count or 0,
            'failure_count': log.failure_count or 0,
        }
        for log in logs
        if log.status == 'processing'
    ]

    return render_template(
        'logs/list.html',
        logs=logs,
        search=search,
        processing_logs=processing_logs,
    )


@bp.route('/logs/<int:log_id>')
@login_required
def log_detail(log_id):
    try:
        log = db.get_or_404(MessageLog, log_id)
    except OperationalError as exc:
        from flask import current_app
        current_app.logger.warning(
            'MessageLog detail query failed due to schema mismatch: %s',
            exc,
        )
        flash('Logs are temporarily unavailable due to a schema mismatch.', 'error')
        return redirect(url_for('main.logs_list'))

    details = []
    if log.details:
        try:
            details = json.loads(log.details)
        except json.JSONDecodeError as exc:
            current_app.logger.warning(
                'MessageLog details JSON decode failed for log_id=%s: %s',
                log_id,
                exc,
            )
            details = []
    phones = set()
    for detail in details:
        raw_phone = detail.get('phone') or detail.get('to') or detail.get('recipient')
        normalized = normalize_phone(raw_phone) if raw_phone else ''
        detail['normalized_phone'] = normalized
        if normalized:
            phones.add(normalized)

    suppression_status = {}
    if phones:
        unsubscribed_phones = {
            entry.phone for entry in UnsubscribedContact.query.filter(UnsubscribedContact.phone.in_(phones))
        }
        suppressed_phones = {
            entry.phone for entry in SuppressedContact.query.filter(SuppressedContact.phone.in_(phones))
        }
        for phone in phones:
            if phone in unsubscribed_phones:
                suppression_status[phone] = 'unsubscribed'
            elif phone in suppressed_phones:
                suppression_status[phone] = 'suppressed'

    return render_template(
        'logs/detail.html',
        log=log,
        details=details,
        suppression_status=suppression_status,
    )


@bp.route('/logs/status')
@login_required
def logs_status():
    """API endpoint for polling message log status changes."""
    ids_str = request.args.get('ids', '').strip()
    if not ids_str:
        return jsonify({'logs': []})
    try:
        ids = [int(i) for i in ids_str.split(',') if i.strip()]
    except ValueError:
        return jsonify({'logs': []})
    if not ids:
        return jsonify({'logs': []})

    ids = ids[:100]
    try:
        logs = MessageLog.query.filter(MessageLog.id.in_(ids)).all()
    except OperationalError as exc:
        current_app.logger.warning(
            'MessageLog status query failed due to schema mismatch: %s',
            exc,
        )
        return jsonify({'logs': []})

    payload = []
    for log in logs:
        payload.append({
            'id': log.id,
            'status': log.status or 'sent',
            'success_count': log.success_count or 0,
            'failure_count': log.failure_count or 0,
        })

    return jsonify({'logs': payload})


# Scheduled Messages
def _apply_scheduled_search_filter(query, search: str):
    if not search:
        return query

    escaped = escape_like(search)
    pattern = f'%{escaped}%'
    return query.outerjoin(Event).filter(
        db.or_(
            ScheduledMessage.message_body.ilike(pattern, escape='\\'),
            ScheduledMessage.target.ilike(pattern, escape='\\'),
            Event.title.ilike(pattern, escape='\\')
        )
    )


@bp.route('/scheduled')
@login_required
def scheduled_list():
    search = request.args.get('search', '').strip()
    now = datetime.utcnow()
    query = _apply_scheduled_search_filter(ScheduledMessage.query, search)

    pending = query.filter(ScheduledMessage.status == 'pending').order_by(ScheduledMessage.scheduled_at).all()
    past = query.filter(ScheduledMessage.status != 'pending').order_by(ScheduledMessage.scheduled_at.desc()).limit(50).all()
    pending_ids = [m.id for m in pending]

    return render_template(
        'scheduled/list.html',
        pending=pending,
        past=past,
        now=now,
        pending_ids=pending_ids,
        search=search
    )


@bp.route('/scheduled/<int:scheduled_id>/cancel', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_cancel(scheduled_id):
    scheduled = db.get_or_404(ScheduledMessage, scheduled_id)
    
    if scheduled.status not in {'pending', 'processing'}:
        flash('Only pending or processing messages can be cancelled.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    scheduled.status = 'cancelled'
    db.session.commit()
    flash('Scheduled message cancelled.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/<int:scheduled_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_delete(scheduled_id):
    scheduled = db.get_or_404(ScheduledMessage, scheduled_id)
    db.session.delete(scheduled)
    db.session.commit()
    flash('Scheduled message deleted.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_bulk_delete():
    ids_str = request.form.get('scheduled_ids', '')
    if not ids_str:
        flash('No messages selected.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    try:
        ids = [int(i) for i in ids_str.split(',') if i.strip()]
    except ValueError:
        flash('Invalid selection.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    deleted = ScheduledMessage.query.filter(ScheduledMessage.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    flash(f'{deleted} scheduled message(s) deleted.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/status')
@login_required
def scheduled_status():
    """API endpoint for polling scheduled message status changes."""
    search = request.args.get('search', '').strip()
    pending = _apply_scheduled_search_filter(
        ScheduledMessage.query,
        search
    ).filter(ScheduledMessage.status == 'pending').all()
    pending_ids = [m.id for m in pending]
    pending_count = len(pending_ids)
    
    # Return current state for comparison
    return jsonify({
        'pending_count': pending_count,
        'pending_ids': pending_ids
    })


@bp.route('/logs/clear', methods=['POST'])
@login_required
@require_roles('admin')
def logs_clear():
    """Clear all message logs - requires admin password confirmation."""
    admin_password = request.form.get('admin_password', '')

    if not current_user.check_password(admin_password):
        flash('Invalid admin password.', 'error')
        return redirect(url_for('main.logs_list'))
    
    # Clear all logs
    deleted_count = MessageLog.query.delete()
    db.session.commit()
    
    flash(f'Successfully cleared {deleted_count} log(s).', 'success')
    return redirect(url_for('main.logs_list'))


# Unsubscribed Contacts
@bp.route('/unsubscribed')
@login_required
def unsubscribed_list():
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    sort_key, sort_dir = normalize_sort_params(
        request.args.get('sort'),
        request.args.get('dir'),
        allowed_keys={'name', 'phone', 'reason', 'category', 'source', 'created_at'},
        default_key='created_at',
        default_dir='desc',
    )
    per_page = 50

    unsubscribed_query = UnsubscribedContact.query
    suppressed_query = SuppressedContact.query
    search_filter_unsubscribed = ''
    search_filter_suppressed = ''
    sql_params = {}

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        sql_params['pattern'] = pattern
        unsubscribed_query = unsubscribed_query.filter(
            db.or_(
                UnsubscribedContact.name.ilike(pattern, escape='\\'),
                UnsubscribedContact.phone.ilike(pattern, escape='\\'),
                UnsubscribedContact.reason.ilike(pattern, escape='\\'),
                UnsubscribedContact.source.ilike(pattern, escape='\\'),
            )
        )
        suppressed_query = suppressed_query.filter(
            db.or_(
                SuppressedContact.phone.ilike(pattern, escape='\\'),
                SuppressedContact.reason.ilike(pattern, escape='\\'),
                SuppressedContact.category.ilike(pattern, escape='\\'),
                SuppressedContact.source.ilike(pattern, escape='\\'),
            )
        )
        search_filter_unsubscribed = """
            AND (
                LOWER(u.name) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.phone) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.reason) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.source) LIKE LOWER(:pattern) ESCAPE '\\'
            )
        """
        search_filter_suppressed = """
            AND (
                LOWER(s.phone) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.reason) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.category) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.source) LIKE LOWER(:pattern) ESCAPE '\\'
            )
        """

    unsubscribed_count = unsubscribed_query.count()
    suppressed_count = suppressed_query.count()
    total_count = unsubscribed_count + suppressed_count
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    offset = (page - 1) * per_page
    sql_params.update({'limit': per_page, 'offset': offset})
    phone_sort_expr = (
        "CAST(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '(', ''), ')', ''), '-', ''), ' ', ''), '.', '') AS BIGINT)"
    )
    sort_config = {
        'name': {'expr': 'LOWER(name)', 'null_check': 'name'},
        'phone': {'expr': phone_sort_expr, 'null_check': 'phone'},
        'reason': {'expr': 'LOWER(reason)', 'null_check': 'reason'},
        'category': {'expr': 'LOWER(category)', 'null_check': 'category'},
        'source': {'expr': 'LOWER(source)', 'null_check': 'source'},
        'created_at': {'expr': 'created_at', 'null_check': 'created_at'},
    }
    sort_expr = sort_config[sort_key]['expr']
    null_check = sort_config[sort_key]['null_check']
    null_rank = 1 if sort_dir == 'asc' else 0
    not_null_rank = 0 if sort_dir == 'asc' else 1
    order_by = (
        f"CASE WHEN {null_check} IS NULL OR {null_check} = '' THEN {null_rank} ELSE {not_null_rank} END, "
        f"{sort_expr} {sort_dir}, "
        "created_at DESC, entry_type, id"
    )

    combined_sql = f"""
        SELECT
            id,
            name,
            phone,
            reason,
            category,
            source,
            created_at,
            entry_type
        FROM (
            SELECT
                u.id AS id,
                u.name AS name,
                u.phone AS phone,
                u.reason AS reason,
                'unsubscribed' AS category,
                u.source AS source,
                u.created_at AS created_at,
                'unsubscribed' AS entry_type
            FROM unsubscribed_contacts u
            WHERE 1 = 1
            {search_filter_unsubscribed}
            UNION ALL
            SELECT
                s.id AS id,
                NULL AS name,
                s.phone AS phone,
                s.reason AS reason,
                s.category AS category,
                s.source AS source,
                s.created_at AS created_at,
                'suppressed' AS entry_type
            FROM suppressed_contacts s
            WHERE 1 = 1
            {search_filter_suppressed}
        ) combined
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset
    """
    combined_query = text(combined_sql).columns(
        id=Integer(),
        name=String(),
        phone=String(),
        reason=Text(),
        category=String(),
        source=String(),
        created_at=DateTime(),
        entry_type=String(),
    )
    combined = [
        dict(row)
        for row in db.session.execute(combined_query, sql_params).mappings().all()
    ]

    return render_template(
        'unsubscribed/list.html',
        entries=combined,
        search=search,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )


@bp.route('/unsubscribed/backfill', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_backfill():
    try:
        from app.queue import get_queue

        queue = get_queue()
        job = queue.enqueue('app.tasks.backfill_suppressions_job')
        message = f"Backfill queued (job {job.id}). Results will appear shortly."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'message': message, 'job_id': job.id})
        flash(message, 'success')
    except Exception:
        current_app.logger.exception('Failed to queue backfill job')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Failed to queue backfill. Check server logs.'}), 500
        flash('Failed to queue backfill. Check server logs.', 'error')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/unsubscribed/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def unsubscribed_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        reason = request.form.get('reason', '').strip() or None
        source = request.form.get('source', '').strip() or 'manual'
        next_url = request.form.get('next')

        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('unsubscribed/form.html', entry=None)

        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('unsubscribed/form.html', entry=None)

        existing = UnsubscribedContact.query.filter_by(phone=phone).first()
        if existing:
            flash('That phone number is already unsubscribed.', 'warning')
            if next_url and _is_safe_url(next_url):
                return redirect(next_url)
            return redirect(url_for('main.unsubscribed_list'))

        entry = UnsubscribedContact(name=name, phone=phone, reason=reason, source=source)
        db.session.add(entry)
        db.session.commit()
        flash('Added to unsubscribed list.', 'success')

        if next_url and _is_safe_url(next_url):
            return redirect(next_url)
        return redirect(url_for('main.unsubscribed_list'))

    return render_template('unsubscribed/form.html', entry=None)


@bp.route('/unsubscribed/import', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def unsubscribed_import():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file uploaded.', 'error')
            return render_template('unsubscribed/import.html')

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return render_template('unsubscribed/import.html')

        try:
            content = file.read().decode('utf-8')
            parsed = parse_recipients_csv(content)

            if not parsed:
                flash('No valid entries found in CSV.', 'error')
                return render_template('unsubscribed/import.html')

            added = 0
            skipped = 0

            for rec in parsed:
                existing = UnsubscribedContact.query.filter_by(phone=rec['phone']).first()
                if existing:
                    skipped += 1
                    continue

                entry = UnsubscribedContact(
                    name=rec['name'],
                    phone=rec['phone'],
                    source='import'
                )
                db.session.add(entry)
                added += 1

            db.session.commit()
            flash(f'Imported {added} unsubscribed contact(s). {skipped} duplicates skipped.', 'success')
            return redirect(url_for('main.unsubscribed_list'))

        except Exception as e:
            flash(f'Error processing CSV: {str(e)}', 'error')

    return render_template('unsubscribed/import.html')


@bp.route('/unsubscribed/export')
@login_required
@require_roles('admin')
def unsubscribed_export():
    entries = UnsubscribedContact.query.order_by(UnsubscribedContact.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'phone', 'reason', 'source', 'created_at'])
    for entry in entries:
        writer.writerow([
            entry.name or '',
            entry.phone,
            entry.reason or '',
            entry.source,
            entry.created_at.isoformat() if entry.created_at else '',
        ])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=unsubscribed_contacts.csv'
    return response


@bp.route('/unsubscribed/<int:entry_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_delete(entry_id):
    entry = db.session.get(UnsubscribedContact, entry_id)
    if entry is None:
        flash('Entry already deleted or not found.', 'warning')
        return redirect(url_for('main.unsubscribed_list'))
    db.session.delete(entry)
    db.session.commit()
    flash('Removed from unsubscribed list.', 'success')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/unsubscribed/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_bulk_delete():
    raw_unsub_ids = request.form.getlist('unsubscribed_ids')
    raw_supp_ids = request.form.getlist('suppressed_ids')

    unsub_ids = []
    for raw in raw_unsub_ids:
        try:
            unsub_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    supp_ids = []
    for raw in raw_supp_ids:
        try:
            supp_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    unsub_ids = sorted(set(unsub_ids))
    supp_ids = sorted(set(supp_ids))

    if not unsub_ids and not supp_ids:
        flash('No entries selected.', 'warning')
        return redirect(url_for('main.unsubscribed_list'))

    deleted_unsub = 0
    deleted_supp = 0

    if unsub_ids:
        deleted_unsub = UnsubscribedContact.query.filter(
            UnsubscribedContact.id.in_(unsub_ids)
        ).delete(synchronize_session=False)

    if supp_ids:
        deleted_supp = SuppressedContact.query.filter(
            SuppressedContact.id.in_(supp_ids)
        ).delete(synchronize_session=False)

    db.session.commit()
    total = deleted_unsub + deleted_supp
    flash(f'Deleted {total} entry/entries ({deleted_unsub} unsubscribed, {deleted_supp} suppressed).', 'success')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/webhooks/twilio/inbound', methods=['POST'])
@csrf.exempt
def twilio_inbound_webhook():
    payload = request.form.to_dict(flat=True)
    if current_app.config.get('TWILIO_VALIDATE_INBOUND_SIGNATURE', True):
        signature = request.headers.get('X-Twilio-Signature')
        if not validate_inbound_signature(request.url, payload, signature):
            current_app.logger.warning('Rejected inbound webhook due to invalid Twilio signature.')
            return 'Forbidden', 403

    try:
        result = process_inbound_sms(payload)
        current_app.logger.info(
            'Inbound webhook processed: status=%s phone=%s thread_id=%s',
            result.get('status'),
            result.get('phone'),
            result.get('thread_id'),
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to process inbound Twilio webhook payload')
        return 'Internal Server Error', 500

    response = make_response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200)
    response.headers['Content-Type'] = 'text/xml'
    return response


@bp.route('/inbox')
@login_required
def inbox_list():
    search = request.args.get('search', '').strip()
    selected_thread_id = request.args.get('thread', type=int)
    query = InboxThread.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = (
            query.outerjoin(CommunityMember, CommunityMember.phone == InboxThread.phone)
            .filter(
                db.or_(
                    InboxThread.phone.ilike(pattern, escape='\\'),
                    InboxThread.contact_name.ilike(pattern, escape='\\'),
                    CommunityMember.name.ilike(pattern, escape='\\'),
                )
            )
            .distinct()
        )

    threads = query.order_by(InboxThread.last_message_at.desc()).limit(200).all()
    selected_thread = None
    if selected_thread_id:
        selected_thread = next((thread for thread in threads if thread.id == selected_thread_id), None)
        if selected_thread is None:
            selected_thread = db.session.get(InboxThread, selected_thread_id)
    elif threads:
        selected_thread = threads[0]

    messages = []
    active_sessions = []
    if selected_thread:
        if selected_thread.unread_count:
            mark_thread_read(selected_thread.id)
            selected_thread.unread_count = 0

        messages = InboxMessage.query.filter_by(thread_id=selected_thread.id).order_by(InboxMessage.created_at.asc()).all()
        active_sessions = SurveySession.query.filter_by(
            thread_id=selected_thread.id,
            status='active',
        ).order_by(SurveySession.started_at.desc()).all()

    thread_display_names = _build_thread_display_names(threads, selected_thread=selected_thread)
    latest_message_id = db.session.query(func.max(InboxMessage.id)).scalar() or 0

    return render_template(
        'inbox/list.html',
        threads=threads,
        selected_thread=selected_thread,
        messages=messages,
        active_sessions=active_sessions,
        thread_display_names=thread_display_names,
        inbox_status_latest_message_id=latest_message_id,
        search=search,
    )


@bp.route('/inbox/status')
@login_required
def inbox_status():
    latest_message_id = db.session.query(func.max(InboxMessage.id)).scalar() or 0
    return jsonify({'latest_message_id': int(latest_message_id)})


@bp.route('/inbox/<int:thread_id>/reply', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_reply(thread_id):
    body = request.form.get('body', '').strip()
    if not body:
        flash('Reply message cannot be empty.', 'error')
        return redirect(url_for('main.inbox_list', thread=thread_id))

    try:
        result = send_thread_reply(thread_id, body, actor=current_user.username)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed sending manual inbox reply.')
        flash('Failed to send reply. Check server logs for details.', 'error')
        return redirect(url_for('main.inbox_list', thread=thread_id))

    if result.get('success'):
        flash('Reply sent.', 'success')
    else:
        error = result.get('error') or 'Unknown error'
        flash(f'Reply could not be delivered: {error}', 'error')

    return redirect(url_for('main.inbox_list', thread=thread_id))


@bp.route('/inbox/threads/<int:thread_id>/update', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_thread_update(thread_id):
    thread = db.get_or_404(InboxThread, thread_id)
    contact_name = request.form.get('contact_name')
    updated = update_thread_contact_name(thread.id, contact_name)
    if updated is None:
        flash('Thread not found.', 'error')
        return _redirect_to_inbox()

    flash('Thread contact updated.', 'success')
    return _redirect_to_inbox(thread_id=thread.id)


@bp.route('/inbox/threads/<int:thread_id>/delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_thread_delete(thread_id):
    thread = db.get_or_404(InboxThread, thread_id)
    result = delete_thread_with_dependencies(thread.id)
    if result is None:
        flash('Thread not found.', 'error')
        return _redirect_to_inbox()

    flash(
        (
            'Thread deleted '
            f"({result['messages']} message(s), "
            f"{result['sessions']} survey session(s), "
            f"{result['responses']} survey response(s))."
        ),
        'success',
    )
    return _redirect_to_inbox()


@bp.route('/inbox/messages/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_messages_bulk_delete():
    thread_id = request.form.get('thread_id', type=int)
    if not thread_id:
        flash('Thread is required.', 'error')
        return _redirect_to_inbox()

    db.get_or_404(InboxThread, thread_id)
    message_ids = _parse_int_ids(request.form.getlist('message_ids'))
    if not message_ids:
        flash('No messages selected.', 'warning')
        return _redirect_to_inbox(thread_id=thread_id)

    deleted = delete_messages_in_thread(thread_id, message_ids)
    flash(f'Deleted {deleted} message(s).', 'success')
    return _redirect_to_inbox(thread_id=thread_id)


@bp.route('/inbox/keywords')
@login_required
@require_roles('admin', 'social_manager')
def keyword_rules_list():
    search = request.args.get('search', '').strip()
    query = KeywordAutomationRule.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                KeywordAutomationRule.keyword.ilike(pattern, escape='\\'),
                KeywordAutomationRule.response_body.ilike(pattern, escape='\\'),
            )
        )

    rules = query.order_by(KeywordAutomationRule.keyword.asc()).all()
    return render_template('inbox/keywords_list.html', rules=rules, search=search)


@bp.route('/inbox/keywords/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_add():
    form_data = {'keyword': '', 'response_body': '', 'is_active': True}
    if request.method == 'POST':
        keyword = request.form.get('keyword', '')
        response_body = request.form.get('response_body', '').strip()
        is_active = request.form.get('is_active') == 'on'
        normalized_keyword = normalize_keyword(keyword)
        form_data = {
            'keyword': keyword,
            'response_body': response_body,
            'is_active': is_active,
        }

        if not normalized_keyword:
            flash('Keyword is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if not response_body:
            flash('Auto-reply message is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if _keyword_conflicts_with_rule(normalized_keyword):
            flash('That keyword already exists.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if _keyword_conflicts_with_survey(normalized_keyword):
            flash('That keyword is already used as a survey trigger.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)

        rule = KeywordAutomationRule(
            keyword=normalized_keyword,
            response_body=response_body,
            is_active=is_active,
        )
        db.session.add(rule)
        db.session.commit()
        flash('Keyword automation created.', 'success')
        return redirect(url_for('main.keyword_rules_list'))

    return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)


@bp.route('/inbox/keywords/<int:rule_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_edit(rule_id):
    rule = db.get_or_404(KeywordAutomationRule, rule_id)

    if request.method == 'POST':
        keyword = request.form.get('keyword', '')
        response_body = request.form.get('response_body', '').strip()
        is_active = request.form.get('is_active') == 'on'
        normalized_keyword = normalize_keyword(keyword)

        if not normalized_keyword:
            flash('Keyword is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)
        if not response_body:
            flash('Auto-reply message is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)

        if _keyword_conflicts_with_rule(normalized_keyword, exclude_rule_id=rule.id):
            flash('That keyword already exists.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)
        if _keyword_conflicts_with_survey(normalized_keyword):
            flash('That keyword is already used as a survey trigger.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)

        rule.keyword = normalized_keyword
        rule.response_body = response_body
        rule.is_active = is_active
        db.session.commit()
        flash('Keyword automation updated.', 'success')
        return redirect(url_for('main.keyword_rules_list'))

    return render_template('inbox/keyword_form.html', rule=rule, form_data=None)


@bp.route('/inbox/keywords/<int:rule_id>/delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_delete(rule_id):
    rule = db.get_or_404(KeywordAutomationRule, rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash('Keyword automation deleted.', 'success')
    return redirect(url_for('main.keyword_rules_list'))


@bp.route('/inbox/surveys')
@login_required
@require_roles('admin', 'social_manager')
def survey_flows_list():
    search = request.args.get('search', '').strip()
    query = SurveyFlow.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                SurveyFlow.name.ilike(pattern, escape='\\'),
                SurveyFlow.trigger_keyword.ilike(pattern, escape='\\'),
                SurveyFlow.intro_message.ilike(pattern, escape='\\'),
                SurveyFlow.completion_message.ilike(pattern, escape='\\'),
            )
        )

    surveys = query.order_by(SurveyFlow.created_at.desc()).all()
    return render_template('inbox/surveys_list.html', surveys=surveys, search=search)


@bp.route('/inbox/surveys/<int:survey_id>/submissions')
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_submissions(survey_id):
    survey = db.get_or_404(SurveyFlow, survey_id)
    search = request.args.get('search', '').strip()
    payload = _build_survey_submission_data(survey, search=search)
    return render_template(
        'inbox/survey_submissions.html',
        survey=survey,
        search=search,
        questions=payload['questions'],
        latest_rows=payload['latest_rows'],
        history_by_phone=payload['history_by_phone'],
        unique_attendees=payload['unique_attendees'],
        total_completed=payload['total_completed'],
        repeat_submitters=payload['repeat_submitters'],
    )


@bp.route('/inbox/surveys/<int:survey_id>/submissions/export')
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_submissions_export(survey_id):
    survey = db.get_or_404(SurveyFlow, survey_id)
    payload = _build_survey_submission_data(survey)
    all_rows = payload['all_completed_rows']
    latest_session_by_phone = payload['latest_session_by_phone']
    questions = payload['questions']

    output = io.StringIO()
    writer = csv.writer(output)

    question_headers = [question if question else f'question_{index + 1}' for index, question in enumerate(questions)]
    writer.writerow(
        [
            'submission_id',
            'phone',
            'display_name',
            'submitted_at_utc',
            'is_latest_for_phone',
            *question_headers,
        ]
    )

    for row in all_rows:
        phone = str(row['phone'])
        submitted_at = row.get('submitted_at')
        submitted_at_utc = submitted_at.isoformat() if submitted_at else ''
        is_latest_for_phone = int(row['session_id']) == int(latest_session_by_phone.get(phone, -1))
        writer.writerow(
            [
                row['session_id'],
                phone,
                row['display_name'],
                submitted_at_utc,
                'true' if is_latest_for_phone else 'false',
                *row.get('answers', []),
            ]
        )

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'survey_{survey.id}_submissions.csv'
    return response


@bp.route('/inbox/surveys/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_add():
    form_data = {
        'name': '',
        'trigger_keyword': '',
        'intro_message': '',
        'completion_message': '',
        'questions': '',
        'is_active': True,
        'event_link_mode': 'none',
        'existing_event_id': '',
        'new_event_title': '',
        'new_event_date': '',
    }
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'
        event_link_mode = (request.form.get('event_link_mode') or 'none').strip().lower()
        if event_link_mode not in {'none', 'existing', 'new'}:
            event_link_mode = 'none'
        existing_event_id = request.form.get('existing_event_id', type=int)
        new_event_title = request.form.get('new_event_title', '').strip()
        new_event_date_raw = request.form.get('new_event_date', '').strip()

        form_data = {
            'name': name,
            'trigger_keyword': trigger_keyword,
            'intro_message': intro_message or '',
            'completion_message': completion_message or '',
            'questions': questions_raw,
            'is_active': is_active,
            'event_link_mode': event_link_mode,
            'existing_event_id': str(existing_event_id or ''),
            'new_event_title': new_event_title,
            'new_event_date': new_event_date_raw,
        }

        linked_event_id = None
        new_event_date = None
        if event_link_mode == 'existing':
            if not existing_event_id:
                flash('Select an existing event to link this survey.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            linked_event = db.session.get(Event, existing_event_id)
            if linked_event is None:
                flash('Selected event was not found.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            linked_event_id = linked_event.id
        elif event_link_mode == 'new':
            if not new_event_title:
                flash('Event title is required when creating a new linked event.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            if new_event_date_raw:
                try:
                    new_event_date = datetime.strptime(new_event_date_raw, '%Y-%m-%d').date()
                except ValueError:
                    flash('Invalid linked event date format.', 'error')
                    return _render_survey_form(survey=None, form_data=form_data)

        if not name:
            flash('Survey name is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if SurveyFlow.query.filter_by(name=name).first():
            flash('A survey with this name already exists.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if _keyword_conflicts_with_survey(trigger_keyword):
            flash('That survey trigger keyword already exists.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if _keyword_conflicts_with_rule(trigger_keyword):
            flash('That survey trigger keyword is already used by a keyword automation.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)

        if event_link_mode == 'new':
            linked_event = Event(title=new_event_title, date=new_event_date)
            db.session.add(linked_event)
            db.session.flush()
            linked_event_id = linked_event.id

        survey = SurveyFlow(
            name=name,
            trigger_keyword=trigger_keyword,
            intro_message=intro_message,
            completion_message=completion_message,
            linked_event_id=linked_event_id,
            is_active=is_active,
        )
        survey.set_questions(questions)
        db.session.add(survey)
        db.session.commit()
        flash('Survey flow created.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return _render_survey_form(survey=None, form_data=form_data)


@bp.route('/inbox/surveys/<int:survey_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_edit(survey_id):
    survey = db.get_or_404(SurveyFlow, survey_id)
    form_data = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'
        event_link_mode = (request.form.get('event_link_mode') or 'none').strip().lower()
        if event_link_mode not in {'none', 'existing', 'new'}:
            event_link_mode = 'none'
        existing_event_id = request.form.get('existing_event_id', type=int)
        new_event_title = request.form.get('new_event_title', '').strip()
        new_event_date_raw = request.form.get('new_event_date', '').strip()

        form_data = {
            'name': name,
            'trigger_keyword': trigger_keyword,
            'intro_message': intro_message or '',
            'completion_message': completion_message or '',
            'questions': questions_raw,
            'is_active': is_active,
            'event_link_mode': event_link_mode,
            'existing_event_id': str(existing_event_id or ''),
            'new_event_title': new_event_title,
            'new_event_date': new_event_date_raw,
        }

        linked_event_id = None
        new_event_date = None
        if event_link_mode == 'existing':
            if not existing_event_id:
                flash('Select an existing event to link this survey.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            linked_event = db.session.get(Event, existing_event_id)
            if linked_event is None:
                flash('Selected event was not found.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            linked_event_id = linked_event.id
        elif event_link_mode == 'new':
            if not new_event_title:
                flash('Event title is required when creating a new linked event.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            if new_event_date_raw:
                try:
                    new_event_date = datetime.strptime(new_event_date_raw, '%Y-%m-%d').date()
                except ValueError:
                    flash('Invalid linked event date format.', 'error')
                    return _render_survey_form(survey=survey, form_data=form_data)

        if not name:
            flash('Survey name is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        name_conflict = SurveyFlow.query.filter(
            SurveyFlow.name == name,
            SurveyFlow.id != survey.id,
        ).first()
        if name_conflict:
            flash('A survey with this name already exists.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        if _keyword_conflicts_with_survey(trigger_keyword, exclude_survey_id=survey.id):
            flash('That survey trigger keyword already exists.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if _keyword_conflicts_with_rule(trigger_keyword):
            flash('That survey trigger keyword is already used by a keyword automation.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        if event_link_mode == 'new':
            linked_event = Event(title=new_event_title, date=new_event_date)
            db.session.add(linked_event)
            db.session.flush()
            linked_event_id = linked_event.id

        survey.name = name
        survey.trigger_keyword = trigger_keyword
        survey.intro_message = intro_message
        survey.completion_message = completion_message
        survey.linked_event_id = linked_event_id
        survey.is_active = is_active
        survey.set_questions(questions)
        db.session.commit()
        flash('Survey flow updated.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return _render_survey_form(survey=survey, form_data=form_data)


@bp.route('/inbox/surveys/<int:survey_id>/deactivate', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_deactivate(survey_id):
    survey = db.get_or_404(SurveyFlow, survey_id)
    survey.is_active = False

    now = utc_now()
    active_sessions = SurveySession.query.filter_by(survey_id=survey.id, status='active').all()
    for session in active_sessions:
        session.status = 'cancelled'
        session.completed_at = now
        session.last_activity_at = now

    db.session.commit()
    flash('Survey flow deactivated.', 'success')
    return redirect(url_for('main.survey_flows_list'))
