import json

from flask import current_app

from app import db
from app.models import (
    InboxMessage,
    InboxThread,
    KeywordAutomationRule,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.twilio_service import get_twilio_service
from app.utils import normalize_keyword, normalize_phone, validate_phone


STOP_KEYWORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT'}
START_KEYWORDS = {'START', 'UNSTOP', 'YES'}
SURVEY_CANCEL_KEYWORDS = {'CANCEL', 'QUIT'}


def keyword_candidates(text: str) -> list[str]:
    normalized = normalize_keyword(text)
    if not normalized:
        return []
    first_word = normalized.split(' ', 1)[0]
    candidates = [normalized]
    if first_word != normalized:
        candidates.append(first_word)
    return candidates


def parse_survey_questions(raw_questions: str) -> list[str]:
    questions = []
    for line in (raw_questions or '').splitlines():
        prompt = line.strip()
        if prompt:
            questions.append(prompt[:320])
    return questions[:10]


def mark_thread_read(thread_id: int) -> None:
    thread = InboxThread.query.get(thread_id)
    if not thread:
        return
    thread.unread_count = 0
    db.session.commit()


def send_thread_reply(thread_id: int, body: str, actor: str | None = None) -> dict:
    thread = InboxThread.query.get(thread_id)
    if thread is None:
        return {'success': False, 'error': 'thread_not_found'}

    reply_body = (body or '').strip()
    if not reply_body:
        return {'success': False, 'error': 'empty_message'}

    try:
        twilio = get_twilio_service()
        result = twilio.send_message(thread.phone, reply_body)
    except Exception as exc:
        result = {'success': False, 'status': 'failed', 'sid': None, 'error': str(exc)}

    _append_inbox_message(
        thread,
        thread.phone,
        'outbound',
        reply_body,
        message_sid=result.get('sid'),
        automation_source='manual',
        raw_payload={'actor': actor} if actor else None,
        delivery_status=result.get('status'),
        delivery_error=result.get('error'),
    )
    db.session.commit()
    return result


def _get_or_create_thread(phone: str, contact_name: str | None = None) -> InboxThread:
    thread = InboxThread.query.filter_by(phone=phone).first()
    if thread is None:
        thread = InboxThread(
            phone=phone,
            contact_name=(contact_name or '').strip() or None,
            unread_count=0,
            last_message_at=utc_now(),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.session.add(thread)
    elif contact_name and not thread.contact_name:
        thread.contact_name = contact_name.strip()
    return thread


def _append_inbox_message(
    thread: InboxThread,
    phone: str,
    direction: str,
    body: str,
    *,
    message_sid: str | None = None,
    automation_source: str | None = None,
    automation_source_id: int | None = None,
    matched_keyword: str | None = None,
    delivery_status: str | None = None,
    delivery_error: str | None = None,
    raw_payload: dict | None = None,
) -> InboxMessage:
    now = utc_now()
    payload = json.dumps(raw_payload, ensure_ascii=True) if raw_payload else None
    resolved_message_sid = (message_sid or '').strip() or None
    if resolved_message_sid:
        existing = InboxMessage.query.filter_by(message_sid=resolved_message_sid).first()
        if existing:
            current_app.logger.warning(
                'Duplicate inbox message SID detected; storing message without SID. sid=%s existing_id=%s direction=%s',
                resolved_message_sid,
                existing.id,
                direction,
            )
            resolved_message_sid = None
    message = InboxMessage(
        thread=thread,
        phone=phone,
        direction=direction,
        body=body or '',
        message_sid=resolved_message_sid,
        automation_source=automation_source,
        automation_source_id=automation_source_id,
        matched_keyword=matched_keyword,
        delivery_status=delivery_status,
        delivery_error=delivery_error,
        raw_payload=payload,
        created_at=now,
    )
    db.session.add(message)
    thread.last_message_at = now
    thread.last_message_preview = (body or '')[:180]
    thread.last_direction = direction
    thread.updated_at = now
    if direction == 'inbound':
        thread.unread_count = (thread.unread_count or 0) + 1
    return message


def _send_automated_reply(
    phone: str,
    thread: InboxThread,
    body: str,
    *,
    source: str,
    source_id: int | None = None,
) -> dict:
    body = (body or '').strip()
    if not body:
        return {'success': False, 'status': 'skipped', 'sid': None, 'error': 'empty_body'}

    if not current_app.config.get('INBOUND_AUTO_REPLY_ENABLED', True):
        result = {'success': False, 'status': 'disabled', 'sid': None, 'error': 'auto_reply_disabled'}
        _append_inbox_message(
            thread,
            phone,
            'outbound',
            body,
            automation_source=source,
            automation_source_id=source_id,
            delivery_status=result['status'],
            delivery_error=result['error'],
        )
        return result

    try:
        twilio = get_twilio_service()
        result = twilio.send_message(phone, body)
    except Exception as exc:
        result = {'success': False, 'status': 'failed', 'sid': None, 'error': str(exc)}

    _append_inbox_message(
        thread,
        phone,
        'outbound',
        body,
        message_sid=result.get('sid'),
        automation_source=source,
        automation_source_id=source_id,
        delivery_status=result.get('status'),
        delivery_error=result.get('error'),
    )
    return result


def _upsert_unsubscribed(phone: str, reason: str) -> None:
    entry = UnsubscribedContact.query.filter_by(phone=phone).first()
    if entry:
        entry.reason = reason or entry.reason
        entry.source = 'inbound'
        return
    db.session.add(
        UnsubscribedContact(
            phone=phone,
            reason=reason or None,
            source='inbound',
        )
    )


def _remove_unsubscribed(phone: str) -> bool:
    entry = UnsubscribedContact.query.filter_by(phone=phone).first()
    if not entry:
        return False
    db.session.delete(entry)
    return True


def _active_session(phone: str) -> SurveySession | None:
    return (
        SurveySession.query.filter_by(phone=phone, status='active')
        .order_by(SurveySession.started_at.desc())
        .first()
    )


def _cancel_active_sessions(phone: str) -> int:
    now = utc_now()
    sessions = SurveySession.query.filter_by(phone=phone, status='active').all()
    for session in sessions:
        session.status = 'cancelled'
        session.completed_at = now
        session.last_activity_at = now
    return len(sessions)


def _start_survey(survey: SurveyFlow, thread: InboxThread, phone: str) -> tuple[SurveySession, list[str]]:
    _cancel_active_sessions(phone)
    now = utc_now()
    session = SurveySession(
        survey=survey,
        thread=thread,
        phone=phone,
        status='active',
        current_question_index=0,
        started_at=now,
        last_activity_at=now,
    )
    survey.start_count = (survey.start_count or 0) + 1
    db.session.add(session)

    replies: list[str] = []
    if survey.intro_message:
        replies.append(survey.intro_message.strip())

    questions = survey.questions
    if questions:
        replies.append(questions[0])
    else:
        session.status = 'completed'
        session.completed_at = now
        survey.completion_count = (survey.completion_count or 0) + 1
        if survey.completion_message:
            replies.append(survey.completion_message.strip())
    return session, replies


def _advance_survey(session: SurveySession, inbound_text: str) -> list[str]:
    now = utc_now()
    survey = session.survey
    questions = survey.questions
    current_index = session.current_question_index or 0

    if current_index < len(questions):
        db.session.add(
            SurveyResponse(
                session=session,
                survey=survey,
                phone=session.phone,
                question_index=current_index,
                question_prompt=questions[current_index],
                answer=inbound_text,
                created_at=now,
            )
        )
        current_index += 1

    session.current_question_index = current_index
    session.last_activity_at = now

    replies: list[str] = []
    if current_index < len(questions):
        replies.append(questions[current_index])
    else:
        session.status = 'completed'
        session.completed_at = now
        survey.completion_count = (survey.completion_count or 0) + 1
        if survey.completion_message:
            replies.append(survey.completion_message.strip())
    return replies


def process_inbound_sms(payload: dict) -> dict:
    raw_from = (payload.get('From') or '').strip()
    inbound_body = (payload.get('Body') or '').strip()
    message_sid = (payload.get('MessageSid') or payload.get('SmsSid') or '').strip() or None
    profile_name = (payload.get('ProfileName') or '').strip()

    if not raw_from:
        return {'status': 'ignored', 'reason': 'missing_from'}

    phone = normalize_phone(raw_from)
    if not validate_phone(phone):
        return {'status': 'ignored', 'reason': 'invalid_phone', 'phone': phone}

    if message_sid:
        existing = InboxMessage.query.filter_by(message_sid=message_sid).first()
        if existing:
            return {'status': 'duplicate', 'thread_id': existing.thread_id}

    thread = _get_or_create_thread(phone, profile_name)
    inbound_message = _append_inbox_message(
        thread,
        phone,
        'inbound',
        inbound_body,
        message_sid=message_sid,
        raw_payload=payload,
    )
    db.session.flush()

    status = 'stored'
    sent_replies: list[dict] = []
    normalized = normalize_keyword(inbound_body)
    matched_keyword: str | None = None

    session = _active_session(phone)
    if normalized in STOP_KEYWORDS:
        _upsert_unsubscribed(phone, 'Inbound STOP keyword received')
        _cancel_active_sessions(phone)
        sent_replies.append(
            {
                'source': 'system',
                'result': _send_automated_reply(
                    phone,
                    thread,
                    'You are unsubscribed and will no longer receive SMS alerts. Reply START to resubscribe.',
                    source='system',
                ),
            }
        )
        status = 'opt_out'
    elif session and normalized in SURVEY_CANCEL_KEYWORDS:
        now = utc_now()
        session.status = 'cancelled'
        session.completed_at = now
        session.last_activity_at = now
        sent_replies.append(
            {
                'source': 'survey',
                'result': _send_automated_reply(
                    phone,
                    thread,
                    'Survey cancelled. Text the survey keyword again anytime to restart.',
                    source='survey',
                    source_id=session.survey_id,
                ),
            }
        )
        status = 'survey_cancelled'
    else:
        if session:
            # Active survey responses should take precedence over generic START/YES opt-in keywords.
            matched_keyword = session.survey.trigger_keyword
            for reply in _advance_survey(session, inbound_body):
                sent_replies.append(
                    {
                        'source': 'survey',
                        'result': _send_automated_reply(
                            phone,
                            thread,
                            reply,
                            source='survey',
                            source_id=session.survey_id,
                        ),
                    }
                )
            status = 'survey_response'
        elif normalized in START_KEYWORDS:
            was_unsubscribed = _remove_unsubscribed(phone)
            start_reply = (
                'You are resubscribed and can receive SMS alerts again.'
                if was_unsubscribed
                else 'You are already subscribed and can receive SMS alerts.'
            )
            sent_replies.append(
                {
                    'source': 'system',
                    'result': _send_automated_reply(
                        phone,
                        thread,
                        start_reply,
                        source='system',
                    ),
                }
            )
            status = 'opt_in'
        else:
            candidates = keyword_candidates(inbound_body)

            survey = None
            for candidate in candidates:
                survey = SurveyFlow.query.filter_by(trigger_keyword=candidate, is_active=True).first()
                if survey:
                    matched_keyword = candidate
                    break

            if survey:
                _session, replies = _start_survey(survey, thread, phone)
                for reply in replies:
                    sent_replies.append(
                        {
                            'source': 'survey',
                            'result': _send_automated_reply(
                                phone,
                                thread,
                                reply,
                                source='survey',
                                source_id=survey.id,
                            ),
                        }
                    )
                status = 'survey_started'
            else:
                rules = {}
                if candidates:
                    matches = KeywordAutomationRule.query.filter(
                        KeywordAutomationRule.is_active.is_(True),
                        KeywordAutomationRule.keyword.in_(candidates),
                    ).all()
                    rules = {rule.keyword: rule for rule in matches}

                for candidate in candidates:
                    rule = rules.get(candidate)
                    if not rule:
                        continue
                    matched_keyword = candidate
                    rule.match_count = (rule.match_count or 0) + 1
                    rule.last_matched_at = utc_now()
                    sent_replies.append(
                        {
                            'source': 'keyword',
                            'result': _send_automated_reply(
                                phone,
                                thread,
                                rule.response_body,
                                source='keyword',
                                source_id=rule.id,
                            ),
                        }
                    )
                    status = 'keyword_reply'
                    break

    if matched_keyword:
        inbound_message.matched_keyword = matched_keyword

    db.session.commit()
    return {
        'status': status,
        'thread_id': thread.id,
        'phone': phone,
        'replies': sent_replies,
    }
