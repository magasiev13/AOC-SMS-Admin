import csv
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, unquote
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import login_required, current_user
from sqlalchemy import DateTime, Integer, String, Text, text
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
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.inbox_service import (
    mark_thread_read,
    normalize_keyword,
    parse_survey_questions,
    process_inbound_sms,
    send_thread_reply,
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
    user = AppUser.query.get_or_404(user_id)

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
    user = AppUser.query.get_or_404(user_id)

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
        query = query.filter(
            db.or_(
                CommunityMember.name.ilike(f'%{escaped}%', escape='\\'),
                CommunityMember.phone.ilike(f'%{escaped}%', escape='\\')
            )
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
    member = CommunityMember.query.get_or_404(member_id)
    
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
    member = CommunityMember.query.get_or_404(member_id)
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
    member = CommunityMember.query.get_or_404(member_id)
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
    event = Event.query.get_or_404(event_id)
    registrations = EventRegistration.query.filter_by(event_id=event_id).order_by(EventRegistration.name, EventRegistration.phone).all()
    unsubscribed_phones = get_unsubscribed_phone_set([reg.phone for reg in registrations])
    return render_template('events/detail.html', event=event, registrations=registrations, unsubscribed_phones=unsubscribed_phones)


@bp.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def event_edit(event_id):
    event = Event.query.get_or_404(event_id)
    
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
    event = Event.query.get_or_404(event_id)
    db.session.delete(event)
    db.session.commit()
    flash('Event deleted.', 'success')
    return redirect(url_for('main.events_list'))


@bp.route('/events/<int:event_id>/register', methods=['POST'])
@login_required
def event_register(event_id):
    event = Event.query.get_or_404(event_id)
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
    event = Event.query.get_or_404(event_id)
    
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
    event = Event.query.get_or_404(event_id)
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
        log = MessageLog.query.get_or_404(log_id)
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
@bp.route('/scheduled')
@login_required
def scheduled_list():
    search = request.args.get('search', '').strip()
    now = datetime.utcnow()
    query = ScheduledMessage.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.outerjoin(Event).filter(
            db.or_(
                ScheduledMessage.message_body.ilike(pattern, escape='\\'),
                ScheduledMessage.target.ilike(pattern, escape='\\'),
                Event.title.ilike(pattern, escape='\\')
            )
        )

    pending = query.filter_by(status='pending').order_by(ScheduledMessage.scheduled_at).all()
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
    scheduled = ScheduledMessage.query.get_or_404(scheduled_id)
    
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
    scheduled = ScheduledMessage.query.get_or_404(scheduled_id)
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
    pending = ScheduledMessage.query.filter_by(status='pending').all()
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
    entry = UnsubscribedContact.query.get(entry_id)
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
        query = query.filter(
            db.or_(
                InboxThread.phone.ilike(pattern, escape='\\'),
                InboxThread.contact_name.ilike(pattern, escape='\\'),
            )
        )

    threads = query.order_by(InboxThread.last_message_at.desc()).limit(200).all()
    selected_thread = None
    if selected_thread_id:
        selected_thread = next((thread for thread in threads if thread.id == selected_thread_id), None)
        if selected_thread is None:
            selected_thread = InboxThread.query.get(selected_thread_id)
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

    return render_template(
        'inbox/list.html',
        threads=threads,
        selected_thread=selected_thread,
        messages=messages,
        active_sessions=active_sessions,
        search=search,
    )


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
        if KeywordAutomationRule.query.filter_by(keyword=normalized_keyword).first():
            flash('That keyword already exists.', 'error')
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
    rule = KeywordAutomationRule.query.get_or_404(rule_id)

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

        existing = KeywordAutomationRule.query.filter(
            KeywordAutomationRule.keyword == normalized_keyword,
            KeywordAutomationRule.id != rule.id,
        ).first()
        if existing:
            flash('That keyword already exists.', 'error')
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
    rule = KeywordAutomationRule.query.get_or_404(rule_id)
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
    }
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'

        form_data = {
            'name': name,
            'trigger_keyword': trigger_keyword,
            'intro_message': intro_message or '',
            'completion_message': completion_message or '',
            'questions': questions_raw,
            'is_active': is_active,
        }

        if not name:
            flash('Survey name is required.', 'error')
            return render_template('inbox/survey_form.html', survey=None, form_data=form_data)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return render_template('inbox/survey_form.html', survey=None, form_data=form_data)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return render_template('inbox/survey_form.html', survey=None, form_data=form_data)
        if SurveyFlow.query.filter_by(name=name).first():
            flash('A survey with this name already exists.', 'error')
            return render_template('inbox/survey_form.html', survey=None, form_data=form_data)
        if SurveyFlow.query.filter_by(trigger_keyword=trigger_keyword).first():
            flash('That survey trigger keyword already exists.', 'error')
            return render_template('inbox/survey_form.html', survey=None, form_data=form_data)

        survey = SurveyFlow(
            name=name,
            trigger_keyword=trigger_keyword,
            intro_message=intro_message,
            completion_message=completion_message,
            is_active=is_active,
        )
        survey.set_questions(questions)
        db.session.add(survey)
        db.session.commit()
        flash('Survey flow created.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return render_template('inbox/survey_form.html', survey=None, form_data=form_data)


@bp.route('/inbox/surveys/<int:survey_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_edit(survey_id):
    survey = SurveyFlow.query.get_or_404(survey_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'

        if not name:
            flash('Survey name is required.', 'error')
            return render_template('inbox/survey_form.html', survey=survey, form_data=None)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return render_template('inbox/survey_form.html', survey=survey, form_data=None)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return render_template('inbox/survey_form.html', survey=survey, form_data=None)

        name_conflict = SurveyFlow.query.filter(
            SurveyFlow.name == name,
            SurveyFlow.id != survey.id,
        ).first()
        if name_conflict:
            flash('A survey with this name already exists.', 'error')
            return render_template('inbox/survey_form.html', survey=survey, form_data=None)

        keyword_conflict = SurveyFlow.query.filter(
            SurveyFlow.trigger_keyword == trigger_keyword,
            SurveyFlow.id != survey.id,
        ).first()
        if keyword_conflict:
            flash('That survey trigger keyword already exists.', 'error')
            return render_template('inbox/survey_form.html', survey=survey, form_data=None)

        survey.name = name
        survey.trigger_keyword = trigger_keyword
        survey.intro_message = intro_message
        survey.completion_message = completion_message
        survey.is_active = is_active
        survey.set_questions(questions)
        db.session.commit()
        flash('Survey flow updated.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return render_template('inbox/survey_form.html', survey=survey, form_data=None)


@bp.route('/inbox/surveys/<int:survey_id>/deactivate', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_deactivate(survey_id):
    survey = SurveyFlow.query.get_or_404(survey_id)
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
