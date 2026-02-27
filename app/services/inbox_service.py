import json
import time

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    CommunityMember,
    EventRegistration,
    InboxMessage,
    InboxThread,
    KeywordAutomationRule,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.suppression_service import classify_failure
from app.services.twilio_service import get_twilio_service
from app.utils import normalize_keyword, normalize_phone, validate_phone


STOP_KEYWORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT'}
START_KEYWORDS = {'START', 'UNSTOP', 'YES'}
SURVEY_CANCEL_KEYWORDS = {'CANCEL', 'QUIT'}
SURVEY_CONFIRM_PREFIX = 'CONFIRM'


class DuplicateMessageSidError(Exception):
    """Raised when an inbound duplicate MessageSid should be dropped."""

    def __init__(self, existing_message: InboxMessage):
        self.existing_message = existing_message
        super().__init__(f"duplicate_message_sid:{existing_message.message_sid}")


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
    thread = db.session.get(InboxThread, thread_id)
    if not thread:
        return
    thread.unread_count = 0
    db.session.commit()


def update_thread_contact_name(thread_id: int, contact_name: str | None) -> InboxThread | None:
    thread = db.session.get(InboxThread, thread_id)
    if thread is None:
        return None

    thread.contact_name = (contact_name or '').strip() or None
    thread.updated_at = utc_now()
    db.session.commit()
    return thread


def _refresh_thread_rollup(thread: InboxThread) -> None:
    latest_message = (
        InboxMessage.query.filter_by(thread_id=thread.id)
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .first()
    )
    if latest_message is None:
        thread.last_message_preview = None
        thread.last_direction = None
        thread.unread_count = 0
        if thread.last_message_at is None:
            thread.last_message_at = utc_now()
        thread.updated_at = utc_now()
        return

    thread.last_message_at = latest_message.created_at or utc_now()
    thread.last_message_preview = (latest_message.body or '')[:180]
    thread.last_direction = latest_message.direction
    inbound_count = InboxMessage.query.filter_by(thread_id=thread.id, direction='inbound').count()
    thread.unread_count = min(thread.unread_count or 0, inbound_count)
    thread.updated_at = utc_now()


def delete_messages_in_thread(thread_id: int, message_ids: list[int]) -> int:
    thread = db.session.get(InboxThread, thread_id)
    if thread is None:
        return 0

    resolved_message_ids = sorted({int(message_id) for message_id in message_ids if message_id})
    if not resolved_message_ids:
        return 0

    messages = InboxMessage.query.filter(
        InboxMessage.thread_id == thread.id,
        InboxMessage.id.in_(resolved_message_ids),
    ).all()
    if not messages:
        return 0

    deleted_count = len(messages)
    for message in messages:
        db.session.delete(message)

    db.session.flush()
    _refresh_thread_rollup(thread)
    db.session.commit()
    return deleted_count


def delete_thread_with_dependencies(thread_id: int) -> dict[str, int] | None:
    thread = db.session.get(InboxThread, thread_id)
    if thread is None:
        return None

    message_count = InboxMessage.query.filter_by(thread_id=thread.id).count()
    sessions = SurveySession.query.filter_by(thread_id=thread.id).all()
    session_ids = [session.id for session in sessions]
    response_count = 0
    if session_ids:
        response_count = SurveyResponse.query.filter(SurveyResponse.session_id.in_(session_ids)).count()

    for session in sessions:
        db.session.delete(session)

    db.session.delete(thread)
    db.session.commit()
    return {
        'threads': 1,
        'messages': message_count,
        'sessions': len(sessions),
        'responses': response_count,
    }


def delete_survey_flow_with_dependencies(survey_id: int) -> dict[str, int] | None:
    survey = db.session.get(SurveyFlow, survey_id)
    if survey is None:
        return None

    session_count = SurveySession.query.filter_by(survey_id=survey.id).count()
    response_count = SurveyResponse.query.filter_by(survey_id=survey.id).count()

    SurveyResponse.query.filter_by(survey_id=survey.id).delete(synchronize_session=False)
    SurveySession.query.filter_by(survey_id=survey.id).delete(synchronize_session=False)
    db.session.delete(survey)
    db.session.commit()
    return {
        'surveys': 1,
        'sessions': session_count,
        'responses': response_count,
    }


def send_thread_reply(thread_id: int, body: str, actor: str | None = None) -> dict:
    thread = db.session.get(InboxThread, thread_id)
    if thread is None:
        return {'success': False, 'error': 'thread_not_found'}

    reply_body = (body or '').strip()
    if not reply_body:
        return {'success': False, 'error': 'empty_message'}

    if _unsubscribed_entry_for_phone(thread.phone):
        return {
            'success': False,
            'status': 'blocked_opt_out',
            'sid': None,
            'error': 'Cannot send to unsubscribed recipient. They must reply START to resubscribe.',
        }

    try:
        twilio = get_twilio_service()
        result = twilio.send_message(thread.phone, reply_body)
    except Exception as exc:
        result = {'success': False, 'status': 'failed', 'sid': None, 'error': str(exc)}

    if not result.get('success'):
        error_text = result.get('error') or ''
        if classify_failure(error_text) == 'opt_out':
            _upsert_unsubscribed(
                thread.phone,
                error_text or 'Manual reply blocked by Twilio opt-out',
                source='message_failure',
                name=_resolve_unsubscribed_name(thread.phone, thread=thread),
            )

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


def _normalize_unsubscribed_name(name: str | None) -> str | None:
    normalized = (name or '').strip()
    if not normalized:
        return None
    return normalized[:100]


def _phone_digits_sql(column):
    normalized = func.replace(column, '+', '')
    for token in ('(', ')', '-', ' ', '.'):
        normalized = func.replace(normalized, token, '')
    return normalized


def _phone_lookup_variants(phone: str) -> list[str]:
    digits = ''.join(char for char in (phone or '') if char.isdigit())
    if not digits:
        return []

    variants: list[str] = [digits]
    if len(digits) == 11 and digits.startswith('1'):
        variants.append(digits[1:])
    elif len(digits) == 10:
        variants.append(f'1{digits}')
    return list(dict.fromkeys(variants))


def _community_member_for_phone(phone: str) -> CommunityMember | None:
    member = CommunityMember.query.filter_by(phone=phone).first()
    if member is not None:
        return member

    variants = _phone_lookup_variants(phone)
    if not variants:
        return None

    return (
        CommunityMember.query
        .filter(_phone_digits_sql(CommunityMember.phone).in_(variants))
        .order_by(CommunityMember.id.desc())
        .first()
    )


def _thread_for_phone(phone: str) -> InboxThread | None:
    thread = InboxThread.query.filter_by(phone=phone).first()
    if thread is not None:
        return thread

    variants = _phone_lookup_variants(phone)
    if not variants:
        return None

    return (
        InboxThread.query
        .filter(_phone_digits_sql(InboxThread.phone).in_(variants))
        .order_by(InboxThread.id.desc())
        .first()
    )


def _event_registrations_for_phone(phone: str) -> list[EventRegistration]:
    registrations = (
        EventRegistration.query.filter_by(phone=phone)
        .order_by(EventRegistration.created_at.desc(), EventRegistration.id.desc())
        .all()
    )
    if registrations:
        return registrations

    variants = _phone_lookup_variants(phone)
    if not variants:
        return []

    return (
        EventRegistration.query
        .filter(_phone_digits_sql(EventRegistration.phone).in_(variants))
        .order_by(EventRegistration.created_at.desc(), EventRegistration.id.desc())
        .all()
    )


def _unsubscribed_entry_for_phone(phone: str) -> UnsubscribedContact | None:
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return None

    entry = UnsubscribedContact.query.filter_by(phone=normalized_phone).first()
    if entry is not None:
        return entry

    variants = _phone_lookup_variants(normalized_phone)
    if not variants:
        return None

    return (
        UnsubscribedContact.query
        .filter(_phone_digits_sql(UnsubscribedContact.phone).in_(variants))
        .order_by(UnsubscribedContact.id.desc())
        .first()
    )


def _resolve_unsubscribed_name(phone: str, thread: InboxThread | None = None) -> str | None:
    community_member = _community_member_for_phone(phone)
    if community_member:
        community_name = _normalize_unsubscribed_name(community_member.name)
        if community_name:
            return community_name

    resolved_thread = thread
    if resolved_thread is None:
        resolved_thread = _thread_for_phone(phone)
    if resolved_thread is not None:
        thread_name = _normalize_unsubscribed_name(resolved_thread.contact_name)
        if thread_name:
            return thread_name

    registrations = _event_registrations_for_phone(phone)
    for registration in registrations:
        registration_name = _normalize_unsubscribed_name(registration.name)
        if registration_name:
            return registration_name

    return None


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
    duplicate_sid_mode: str = 'store_without_sid',
) -> InboxMessage:
    now = utc_now()
    payload = json.dumps(raw_payload, ensure_ascii=True) if raw_payload else None
    resolved_message_sid = (message_sid or '').strip() or None
    if resolved_message_sid:
        existing = InboxMessage.query.filter_by(message_sid=resolved_message_sid).first()
        if existing:
            if duplicate_sid_mode == 'reject':
                current_app.logger.info(
                    'Duplicate inbox message SID detected; dropping duplicate message. sid=%s existing_id=%s direction=%s',
                    resolved_message_sid,
                    existing.id,
                    direction,
                )
                raise DuplicateMessageSidError(existing)
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


def _survey_start_question_delay_seconds() -> float:
    default_delay = 0.0 if current_app.testing else 1.0
    configured_delay = current_app.config.get('SURVEY_START_QUESTION_DELAY_SECONDS', default_delay)
    try:
        delay_seconds = float(configured_delay)
    except (TypeError, ValueError):
        return default_delay
    return max(0.0, delay_seconds)


def _upsert_unsubscribed(
    phone: str,
    reason: str,
    *,
    source: str = 'inbound',
    name: str | None = None,
) -> None:
    normalized_phone = normalize_phone(phone)
    if not validate_phone(normalized_phone):
        return

    resolved_name = _normalize_unsubscribed_name(name)
    entry = UnsubscribedContact.query.filter_by(phone=normalized_phone).first()
    if entry is None:
        entry = _unsubscribed_entry_for_phone(normalized_phone)
    if entry:
        entry.phone = normalized_phone
        entry.reason = reason or entry.reason
        entry.source = source or entry.source
        if not entry.name and resolved_name:
            entry.name = resolved_name
        return
    db.session.add(
        UnsubscribedContact(
            name=resolved_name,
            phone=normalized_phone,
            reason=reason or None,
            source=source or 'inbound',
        )
    )


def _remove_unsubscribed(phone: str) -> bool:
    entry = _unsubscribed_entry_for_phone(phone)
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


def _event_registration_name_for_session(session: SurveySession) -> str | None:
    for response in session.responses:
        answer = (response.answer or '').strip()
        if answer:
            return answer[:100]
    if session.thread:
        thread_name = (session.thread.contact_name or '').strip()
        if thread_name:
            return thread_name[:100]
    return None


def _sync_linked_event_registration(session: SurveySession) -> None:
    survey = session.survey
    if not survey or not survey.linked_event_id:
        return

    registration_name = _event_registration_name_for_session(session)
    existing = EventRegistration.query.filter_by(
        event_id=survey.linked_event_id,
        phone=session.phone,
    ).first()
    if existing:
        if registration_name and existing.name != registration_name:
            existing.name = registration_name
        return

    db.session.add(
        EventRegistration(
            event_id=survey.linked_event_id,
            name=registration_name,
            phone=session.phone,
        )
    )


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
        _sync_linked_event_registration(session)
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
        _sync_linked_event_registration(session)
        if survey.completion_message:
            replies.append(survey.completion_message.strip())
    return replies


def _extract_inbound_message_sid(payload: dict) -> str | None:
    for key in ('MessageSid', 'SmsSid', 'SmsMessageSid'):
        value = (payload.get(key) or '').strip()
        if value:
            return value
    return None


def _extract_confirmed_survey_answer(inbound_text: str) -> str | None:
    stripped = (inbound_text or '').strip()
    if not stripped:
        return None

    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    if parts[0].upper() != SURVEY_CONFIRM_PREFIX:
        return None

    answer = parts[1].strip()
    return answer or None


def _extract_confirmation_prompt_answer(message_body: str) -> str | None:
    marker = f'reply: {SURVEY_CONFIRM_PREFIX} '
    body = (message_body or '').strip()
    if not body:
        return None

    marker_index = body.rfind(marker)
    if marker_index == -1:
        return None

    answer = body[marker_index + len(marker):].strip()
    return answer or None


def _has_pending_survey_confirmation(session: SurveySession) -> bool:
    latest_survey_outbound = (
        InboxMessage.query.filter(
            InboxMessage.thread_id == session.thread_id,
            InboxMessage.direction == 'outbound',
            InboxMessage.automation_source == 'survey',
            InboxMessage.automation_source_id == session.survey_id,
        )
        .order_by(InboxMessage.id.desc())
        .first()
    )
    if latest_survey_outbound is None:
        return False

    return _extract_confirmation_prompt_answer(latest_survey_outbound.body or '') is not None


def _is_ambiguous_rapid_repeat_survey_answer(
    session: SurveySession,
    inbound_message: InboxMessage,
    inbound_text: str,
    window_seconds: int,
) -> bool:
    candidate_answer = (inbound_text or '').strip()
    if window_seconds <= 0 or not candidate_answer:
        return False

    current_index = session.current_question_index or 0
    if current_index <= 0:
        return False

    inbound_sid = (inbound_message.message_sid or '').strip() or None
    if not inbound_sid:
        return False

    latest_response = (
        SurveyResponse.query.filter_by(session_id=session.id)
        .order_by(SurveyResponse.question_index.desc(), SurveyResponse.id.desc())
        .first()
    )
    if latest_response is None:
        return False
    if latest_response.question_index != current_index - 1:
        return False
    if (latest_response.answer or '').strip() != candidate_answer:
        return False
    if latest_response.created_at is None:
        return False

    now = utc_now()
    response_created_at = latest_response.created_at
    if response_created_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif response_created_at.tzinfo is not None and now.tzinfo is None:
        response_created_at = response_created_at.replace(tzinfo=None)

    elapsed_seconds = (now - response_created_at).total_seconds()
    if elapsed_seconds < 0 or elapsed_seconds > window_seconds:
        return False

    previous_inbound = (
        InboxMessage.query.filter(
            InboxMessage.thread_id == session.thread_id,
            InboxMessage.direction == 'inbound',
            InboxMessage.id < inbound_message.id,
        )
        .order_by(InboxMessage.id.desc())
        .first()
    )
    if previous_inbound is None:
        return False
    if (previous_inbound.body or '').strip() != candidate_answer:
        return False

    previous_sid = (previous_inbound.message_sid or '').strip() or None
    if not previous_sid:
        return False

    return previous_sid != inbound_sid


def _survey_confirmation_prompt(session: SurveySession, candidate_answer: str) -> str:
    compact_answer = ' '.join((candidate_answer or '').split())[:80]
    questions = session.survey.questions
    current_index = session.current_question_index or 0
    question_prompt = questions[current_index] if current_index < len(questions) else 'the current question'
    return (
        f'We received two identical replies quickly. '
        f'If "{compact_answer}" is your answer to "{question_prompt}", '
        f'reply: {SURVEY_CONFIRM_PREFIX} {compact_answer}'
    )


def process_inbound_sms(payload: dict) -> dict:
    raw_from = (payload.get('From') or '').strip()
    inbound_body = (payload.get('Body') or '').strip()
    message_sid = _extract_inbound_message_sid(payload)
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
    try:
        inbound_message = _append_inbox_message(
            thread,
            phone,
            'inbound',
            inbound_body,
            message_sid=message_sid,
            raw_payload=payload,
            duplicate_sid_mode='reject',
        )
        db.session.flush()
    except DuplicateMessageSidError as exc:
        db.session.rollback()
        return {'status': 'duplicate', 'thread_id': exc.existing_message.thread_id}
    except IntegrityError:
        db.session.rollback()
        if message_sid:
            existing = InboxMessage.query.filter_by(message_sid=message_sid).first()
            if existing:
                return {'status': 'duplicate', 'thread_id': existing.thread_id}
        raise

    status = 'stored'
    pending_replies: list[dict[str, object]] = []
    sent_replies: list[dict] = []
    normalized = normalize_keyword(inbound_body)
    matched_keyword: str | None = None
    raw_duplicate_window = current_app.config.get('SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS', 3)
    try:
        duplicate_window_seconds = max(0, int(raw_duplicate_window))
    except (TypeError, ValueError):
        duplicate_window_seconds = 3

    session = _active_session(phone)
    if normalized in STOP_KEYWORDS:
        _upsert_unsubscribed(
            phone,
            'Inbound STOP keyword received',
            source='inbound',
            name=_resolve_unsubscribed_name(phone, thread=thread),
        )
        _cancel_active_sessions(phone)
        status = 'opt_out'
    elif session and normalized in SURVEY_CANCEL_KEYWORDS:
        now = utc_now()
        session.status = 'cancelled'
        session.completed_at = now
        session.last_activity_at = now
        pending_replies.append(
            {
                'source': 'survey',
                'source_id': session.survey_id,
                'body': 'Survey cancelled. Text the survey keyword again anytime to restart.',
            }
        )
        status = 'survey_cancelled'
    elif session and not inbound_body:
        status = 'survey_ignored_empty'
    else:
        if session:
            # Active survey responses should take precedence over generic START/YES opt-in keywords.
            matched_keyword = session.survey.trigger_keyword
            has_pending_confirmation = _has_pending_survey_confirmation(session)
            confirmed_answer = (
                _extract_confirmed_survey_answer(inbound_body)
                if has_pending_confirmation
                else None
            )
            answer_to_record = confirmed_answer or inbound_body

            if confirmed_answer is None and _is_ambiguous_rapid_repeat_survey_answer(
                session,
                inbound_message,
                inbound_body,
                duplicate_window_seconds,
            ):
                current_app.logger.info(
                    'Ambiguous rapid duplicate survey answer requires confirmation. session_id=%s phone=%s question_index=%s sid=%s',
                    session.id,
                    phone,
                    session.current_question_index,
                    message_sid,
                )
                pending_replies.append(
                    {
                        'source': 'survey',
                        'source_id': session.survey_id,
                        'body': _survey_confirmation_prompt(session, inbound_body),
                    }
                )
                status = 'survey_confirmation_required'
            else:
                for reply in _advance_survey(session, answer_to_record):
                    pending_replies.append(
                        {
                            'source': 'survey',
                            'source_id': session.survey_id,
                            'body': reply,
                        }
                    )
                status = 'survey_response'
        elif normalized in START_KEYWORDS and (
            normalized != 'YES' or _unsubscribed_entry_for_phone(phone) is not None
        ):
            was_unsubscribed = _remove_unsubscribed(phone)
            start_reply = (
                'You are resubscribed and can receive SMS alerts again.'
                if was_unsubscribed
                else 'You are already subscribed and can receive SMS alerts.'
            )
            pending_replies.append(
                {
                    'source': 'system',
                    'source_id': None,
                    'body': start_reply,
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
                    pending_replies.append(
                        {
                            'source': 'survey',
                            'source_id': survey.id,
                            'body': reply,
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
                    pending_replies.append(
                        {
                            'source': 'keyword',
                            'source_id': rule.id,
                            'body': rule.response_body,
                        }
                    )
                    status = 'keyword_reply'
                    break

                if status == 'stored' and normalized == 'YES':
                    was_unsubscribed = _remove_unsubscribed(phone)
                    start_reply = (
                        'You are resubscribed and can receive SMS alerts again.'
                        if was_unsubscribed
                        else 'You are already subscribed and can receive SMS alerts.'
                    )
                    pending_replies.append(
                        {
                            'source': 'system',
                            'source_id': None,
                            'body': start_reply,
                        }
                    )
                    status = 'opt_in'

    if matched_keyword:
        inbound_message.matched_keyword = matched_keyword

    # Commit survey/session state before sending outbound messages so retries
    # cannot advance the survey twice if Twilio retries the same webhook.
    db.session.commit()

    survey_reply_count = 0
    for pending in pending_replies:
        source = str(pending['source'])
        source_id = pending.get('source_id')
        body = str(pending.get('body') or '')

        if status == 'survey_started' and source == 'survey':
            if survey_reply_count > 0:
                delay_seconds = _survey_start_question_delay_seconds()
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
            survey_reply_count += 1

        sent_replies.append(
            {
                'source': source,
                'result': _send_automated_reply(
                    phone,
                    thread,
                    body,
                    source=source,
                    source_id=source_id if isinstance(source_id, int) else None,
                ),
            }
        )

    db.session.commit()
    return {
        'status': status,
        'thread_id': thread.id,
        'phone': phone,
        'replies': sent_replies,
    }
