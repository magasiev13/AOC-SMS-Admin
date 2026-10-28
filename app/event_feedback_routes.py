"""Focused event-feedback workflow on the existing keyword/survey engine."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import require_roles
from app.models import Event
from app.services.event_feedback_service import (
    defaults_for_event, feedback_invitation_text, feedback_query, feedback_summary,
    form_values, get_event_feedback_or_404, save_event_feedback,
)


bp = Blueprint('event_feedback', __name__)


def _event(event_id):
    from app.routes import _tenant_get_or_404
    return _tenant_get_or_404(Event, event_id)


def _display_context():
    from app.services.contact_privacy_service import can_view_phone_numbers, redact_phone_numbers
    return {'feedback_display': (lambda value: str(value or '')) if can_view_phone_numbers() else redact_phone_numbers}


def _values():
    return {name: request.form.get(name, '') for name in
            ('name', 'trigger_keyword', 'intro_message', 'questions', 'completion_message')} | {
        'is_active': request.form.get('is_active') == 'on',
    }


@bp.route('/events/<int:event_id>/feedback')
@login_required
@require_roles('admin', 'social_manager')
def index(event_id):
    event = _event(event_id)
    query = feedback_query(event)
    count = query.count()
    pages = max(1, (count + 24) // 25)
    page = min(max(1, request.args.get('page', 1, type=int) or 1), pages)
    flows = query.order_by(db.desc('created_at'), db.desc('id')).offset((page-1)*25).limit(25).all()
    return render_template('feedback/index.html', event=event, flows=flows,
                           page=page, pages=pages, total=count, **_display_context())


@bp.route('/events/<int:event_id>/feedback/new', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def create(event_id):
    event = _event(event_id)
    values = _values() if request.method == 'POST' else defaults_for_event(event)
    if request.method == 'POST':
        try:
            flow = save_event_feedback(event, values)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        except IntegrityError:
            db.session.rollback()
            flash('A feedback name or keyword was saved at the same time. Choose another and try again.', 'error')
        else:
            flash('Event feedback created. Review the invitation before sending it.', 'success')
            return redirect(url_for('event_feedback.detail', event_id=event.id, survey_id=flow.id))
    return render_template('feedback/form.html', event=event, flow=None, form_data=values, **_display_context())


@bp.route('/events/<int:event_id>/feedback/<int:survey_id>')
@login_required
@require_roles('admin', 'social_manager')
def detail(event_id, survey_id):
    event = _event(event_id)
    flow = get_event_feedback_or_404(event, survey_id)
    return render_template('feedback/detail.html', event=event, flow=flow,
                           invitation=feedback_invitation_text(flow, event),
                           summary=feedback_summary(event, flow, page=request.args.get('page', 1, type=int) or 1),
                           **_display_context())


@bp.route('/events/<int:event_id>/feedback/<int:survey_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def edit(event_id, survey_id):
    event = _event(event_id)
    flow = get_event_feedback_or_404(event, survey_id)
    from app.services.contact_privacy_service import staff_phone_privacy_applies, redact_phone_numbers
    if staff_phone_privacy_applies() and any(
        redact_phone_numbers(value) != str(value or '')
        for value in (flow.name, flow.trigger_keyword, flow.intro_message,
                      flow.completion_message, *flow.questions)
    ):
        # A redacted editor must not silently overwrite protected stored text.
        # Owners can edit that content without exposing it to staff first.
        abort(403)
    values = _values() if request.method == 'POST' else form_values(flow)
    if request.method == 'POST':
        try:
            save_event_feedback(event, values, flow=flow)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        except IntegrityError:
            db.session.rollback()
            flash('A feedback name or keyword was saved at the same time. Choose another and try again.', 'error')
        else:
            flash('Feedback updated. Conversations already started keep their original questions and event context.', 'success')
            return redirect(url_for('event_feedback.detail', event_id=event.id, survey_id=flow.id))
    return render_template('feedback/form.html', event=event, flow=flow, form_data=values, **_display_context())


@bp.route('/events/<int:event_id>/feedback/<int:survey_id>/archive', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def archive(event_id, survey_id):
    event = _event(event_id)
    flow = get_event_feedback_or_404(event, survey_id)
    flow.is_active = False
    db.session.commit()
    flash('Feedback paused. Responses are retained, and people cannot start a new conversation with its keyword.', 'success')
    return redirect(url_for('event_feedback.detail', event_id=event.id, survey_id=flow.id))
