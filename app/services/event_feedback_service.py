"""Event feedback is a survey outcome, never an attendance or contact update."""
from __future__ import annotations

from flask import abort
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app import db
from app.models import CommunityMember, Event, SurveyFlow, SurveySession
from app.services.inbox_service import parse_survey_questions
from app.utils import normalize_keyword, normalize_sms_body


DEFAULT_QUESTIONS = (
    'How was the event? Reply with a rating from 1 to 5.',
    'What did you enjoy, and what could we improve?',
)


def feedback_invitation_text(flow: SurveyFlow, event: Event) -> str:
    """Suggested message only; the normal signed send review owns dispatch."""
    if not flow.is_event_feedback or flow.feedback_event_id != event.id:
        raise ValueError('Feedback does not belong to this event.')
    if flow.organization_id != event.organization_id:
        raise ValueError('Feedback and event must share a workspace.')
    return f'Thanks for joining {event.title}! We would love your feedback. Reply {flow.trigger_keyword} to answer {len(flow.questions)} short questions.'


def feedback_query(event: Event):
    return SurveyFlow.query.filter_by(
        purpose='event_feedback', feedback_event_id=event.id, organization_id=event.organization_id,
    )


def event_has_feedback(event: Event) -> bool:
    return feedback_query(event).first() is not None


def get_event_feedback_or_404(event: Event, survey_id: int) -> SurveyFlow:
    flow = feedback_query(event).filter(SurveyFlow.id == survey_id).first()
    if flow is None:
        abort(404)
    return flow


def defaults_for_event(event: Event) -> dict:
    return {
        'name': f'{event.title[:100]} feedback',
        'trigger_keyword': f'FEEDBACK{event.id}',
        'intro_message': 'Thank you for helping us improve. Your answers are feedback, not a new registration.',
        'questions': '\n'.join(DEFAULT_QUESTIONS),
        'completion_message': 'Thank you for your feedback!',
        'is_active': True,
    }


def form_values(flow: SurveyFlow) -> dict:
    return {
        'name': flow.name, 'trigger_keyword': flow.trigger_keyword,
        'intro_message': flow.intro_message or '', 'questions': '\n'.join(flow.questions),
        'completion_message': flow.completion_message or '', 'is_active': flow.is_active,
    }


def save_event_feedback(event: Event, values: dict, *, flow: SurveyFlow | None = None) -> SurveyFlow:
    """Validate before mutation, preserving raw form input on any error."""
    from app.routes import _keyword_conflicts_with_rule, _keyword_conflicts_with_survey

    if flow is not None and (not flow.is_event_feedback or flow.feedback_event_id != event.id
                             or flow.organization_id != event.organization_id):
        raise ValueError('Feedback does not belong to this event.')
    name = str(values.get('name') or '').strip()
    keyword = normalize_keyword(str(values.get('trigger_keyword') or ''))
    questions = parse_survey_questions(str(values.get('questions') or ''))
    intro = normalize_sms_body(str(values.get('intro_message') or '').strip()) or None
    completion = normalize_sms_body(str(values.get('completion_message') or '').strip()) or None
    active = bool(values.get('is_active'))
    if not name or len(name) > 120:
        raise ValueError('Choose a feedback name of at most 120 characters.')
    if not keyword or len(keyword) > 40:
        raise ValueError('Choose a trigger keyword of at most 40 characters.')
    if keyword.split(' ', 1)[0] in {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT', 'START', 'UNSTOP', 'YES', 'HELP'}:
        raise ValueError('Choose a feedback keyword that is not a messaging control word.')
    if not questions:
        raise ValueError('Add at least one feedback question.')
    if (intro and len(intro) > 1600) or (completion and len(completion) > 1600):
        raise ValueError('Intro and completion messages may contain at most 1600 characters each.')
    duplicate = SurveyFlow.query.filter_by(organization_id=event.organization_id, name=name)
    if flow is not None:
        duplicate = duplicate.filter(SurveyFlow.id != flow.id)
    if duplicate.first() is not None:
        raise ValueError('A survey or feedback form with this name already exists.')
    trigger_changed = flow is None or normalize_keyword(flow.trigger_keyword) != keyword
    reactivating = flow is not None and active and not flow.is_active
    if trigger_changed or reactivating:
        if _keyword_conflicts_with_survey(keyword, exclude_survey_id=flow.id if flow is not None else None):
            raise ValueError('That trigger is already used by a survey or feedback form.')
        if _keyword_conflicts_with_rule(keyword):
            raise ValueError('That trigger is already used by a keyword automation.')
    if flow is None:
        flow = SurveyFlow(organization_id=event.organization_id, purpose='event_feedback', feedback_event_id=event.id)
        db.session.add(flow)
    flow.name, flow.trigger_keyword = name, keyword
    flow.intro_message, flow.completion_message = intro, completion
    flow.linked_event_id = None
    flow.is_active = active
    flow.set_questions(questions)
    return flow


def feedback_summary(event: Event, flow: SurveyFlow, *, page: int = 1) -> dict:
    """Bounded response DTOs use session/thread IDs, never serialized phone keys."""
    from app.services.contact_privacy_service import (
        can_view_phone_numbers, contact_display_label, redact_phone_numbers,
    )

    visible = can_view_phone_numbers()
    display = (lambda value: str(value or '')) if visible else redact_phone_numbers
    query = SurveySession.query.filter_by(
        organization_id=event.organization_id, survey_id=flow.id,
        purpose='event_feedback', feedback_event_id=event.id,
    )
    started = query.count()
    completed = query.filter_by(status='completed').count()
    respondents = query.with_entities(func.count(func.distinct(SurveySession.phone))).scalar() or 0
    per_page = 25
    pages = max(1, (started + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    sessions = query.options(selectinload(SurveySession.responses), selectinload(SurveySession.thread)).order_by(
        SurveySession.started_at.desc(), SurveySession.id.desc(),
    ).offset((page - 1) * per_page).limit(per_page).all()
    names = {row.phone: row.name for row in CommunityMember.query.filter(
        CommunityMember.organization_id == event.organization_id,
        CommunityMember.phone.in_([session.phone for session in sessions]),
    ).all()} if sessions else {}
    rows = []
    for session in sessions:
        name = names.get(session.phone) or (session.thread.contact_name if session.thread else None)
        rows.append({
            'id': session.id, 'thread_id': session.thread_id,
            'label': contact_display_label(name, session.id, kind='Respondent'),
            'status': session.status, 'started_at': session.started_at,
            'completed_at': session.completed_at,
            'event_title': display(session.feedback_event_title or event.title),
            'answers': [{'prompt': display(response.question_prompt), 'answer': display(response.answer)}
                        for response in session.responses],
        })
    return {'started': started, 'completed': completed, 'respondents': respondents,
            'rows': rows, 'page': page, 'pages': pages, 'per_page': per_page}
