import csv
import io
import json
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response
from flask_login import login_required, current_user
from app import db
from app.auth import require_roles
from sqlalchemy.exc import OperationalError

from app.models import AppUser, CommunityMember, Event, EventRegistration, MessageLog, ScheduledMessage, UnsubscribedContact
from app.services.recipient_service import filter_unsubscribed_recipients, get_unsubscribed_phone_set
from app.utils import normalize_phone, validate_phone, parse_recipients_csv

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
    events = Event.query.order_by(Event.date.desc()).all()
    admin_test_phone = current_app.config.get('ADMIN_TEST_PHONE')

    def build_dashboard_context():
        community_count = CommunityMember.query.count()
        event_registration_count = EventRegistration.query.count()
        total_recipients = community_count + event_registration_count
        unsubscribed_count = UnsubscribedContact.query.count()
        latest_log = None
        recent_logs = []
        try:
            latest_log = MessageLog.query.order_by(MessageLog.created_at.desc()).first()
            recent_logs = MessageLog.query.order_by(MessageLog.created_at.desc()).limit(5).all()
        except OperationalError as exc:
            from flask import current_app
            db.session.rollback()
            current_app.logger.warning(
                'MessageLog query failed due to schema mismatch: %s',
                exc,
            )
        pending_scheduled_count = ScheduledMessage.query.filter_by(status='pending').count()
        success_rate = None
        failure_rate = None
        if latest_log and latest_log.total_recipients:
            success_rate = round((latest_log.success_count / latest_log.total_recipients) * 100, 1)
            failure_rate = round((latest_log.failure_count / latest_log.total_recipients) * 100, 1)

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
        from datetime import datetime
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
        
        if target == 'event' and not event_id:
            flash('Please select an event.', 'error')
            return render_dashboard()
        
        # Handle scheduled message
        if schedule_later:
            if not schedule_date or not schedule_time:
                flash('Schedule date and time are required.', 'error')
                return render_dashboard()
            
            try:
                from datetime import timezone

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
        must_change_password = request.form.get('must_change_password', 'on') == 'on'

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
        query = query.filter(
            db.or_(
                CommunityMember.name.ilike(f'%{search}%'),
                CommunityMember.phone.ilike(f'%{search}%')
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
        pattern = f'%{search}%'
        query = query.filter(
            db.or_(
                Event.title.ilike(pattern),
                db.cast(Event.date, db.String).ilike(pattern)
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
        pattern = f'%{search}%'
        query = query.outerjoin(Event).filter(
            db.or_(
                MessageLog.message_body.ilike(pattern),
                MessageLog.target.ilike(pattern),
                Event.title.ilike(pattern)
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
    return render_template('logs/list.html', logs=logs, search=search)


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

    details = json.loads(log.details) if log.details else []
    return render_template('logs/detail.html', log=log, details=details)


# Scheduled Messages
@bp.route('/scheduled')
@login_required
def scheduled_list():
    from datetime import datetime, timezone
    from flask import current_app

    search = request.args.get('search', '').strip()
    app_timezone = current_app.config.get('APP_TIMEZONE', 'UTC')
    try:
        tz = ZoneInfo(app_timezone)
    except Exception:
        tz = timezone.utc

    now = datetime.utcnow()
    query = ScheduledMessage.query

    if search:
        pattern = f'%{search}%'
        query = query.outerjoin(Event).filter(
            db.or_(
                ScheduledMessage.message_body.ilike(pattern),
                ScheduledMessage.target.ilike(pattern),
                Event.title.ilike(pattern)
            )
        )

    pending = query.filter_by(status='pending').order_by(ScheduledMessage.scheduled_at).all()
    past = query.filter(ScheduledMessage.status != 'pending').order_by(ScheduledMessage.scheduled_at.desc()).limit(50).all()
    pending_ids = [m.id for m in pending]

    for msg in pending + past:
        if msg.scheduled_at:
            scheduled_utc = msg.scheduled_at
            if scheduled_utc.tzinfo is None:
                scheduled_utc = scheduled_utc.replace(tzinfo=timezone.utc)
            msg.scheduled_at_local = scheduled_utc.astimezone(tz)

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
def scheduled_delete(scheduled_id):
    scheduled = ScheduledMessage.query.get_or_404(scheduled_id)
    db.session.delete(scheduled)
    db.session.commit()
    flash('Scheduled message deleted.', 'success')
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
    query = UnsubscribedContact.query

    if search:
        query = query.filter(
            db.or_(
                UnsubscribedContact.name.ilike(f'%{search}%'),
                UnsubscribedContact.phone.ilike(f'%{search}%'),
                UnsubscribedContact.source.ilike(f'%{search}%')
            )
        )

    unsubscribed = query.order_by(UnsubscribedContact.created_at.desc()).all()
    return render_template('unsubscribed/list.html', unsubscribed=unsubscribed, search=search)


@bp.route('/unsubscribed/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def unsubscribed_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
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

        entry = UnsubscribedContact(name=name, phone=phone, source=source)
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
    writer.writerow(['name', 'phone', 'source', 'created_at'])
    for entry in entries:
        writer.writerow([entry.name or '', entry.phone, entry.source, entry.created_at.isoformat() if entry.created_at else ''])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=unsubscribed_contacts.csv'
    return response


@bp.route('/unsubscribed/<int:entry_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_delete(entry_id):
    entry = UnsubscribedContact.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash('Removed from unsubscribed list.', 'success')
    return redirect(url_for('main.unsubscribed_list'))
