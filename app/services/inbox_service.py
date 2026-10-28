import json
import hashlib
import time
from contextlib import nullcontext
from uuid import uuid4

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    CommunityMember,
    EventRegistration,
    InboxMessage,
    InboxThread,
    MessageDispatchAttempt,
    OutboundRequest,
    KeywordAutomationRule,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.suppression_service import classify_failure
from app.services.twilio_service import (
    get_twilio_service,
    record_usage_candidates,
    resolve_messaging_profile,
)
from app.tenant import organization_context, saas_mode_enabled
from app.utils import (
    normalize_keyword,
    normalize_phone,
    normalize_sms_body,
    phone_digits_sql,
    phone_lookup_variants,
    validate_phone,
)


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
            if len(prompt) > 320:
                raise ValueError(f'Question {len(questions) + 1} has {len(prompt)} characters. Each question may contain at most 320 characters; shorten it before saving.')
            questions.append(prompt)
    if len(questions) > 10:
        raise ValueError(f'This survey has {len(questions)} questions. Use at most 10 questions; no questions were removed or saved.')
    return questions


def mark_thread_read(thread_id: int) -> None:
    thread = InboxThread.query.filter_by(id=thread_id).first()
    if not thread:
        return
    thread.unread_count = 0
    db.session.commit()


def update_thread_contact_name(thread_id: int, contact_name: str | None) -> InboxThread | None:
    thread = InboxThread.query.filter_by(id=thread_id).first()
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
    thread = InboxThread.query.filter_by(id=thread_id).first()
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
    thread = InboxThread.query.filter_by(id=thread_id).first()
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
    survey = SurveyFlow.query.filter_by(id=survey_id).first()
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


def _send_thread_reply_with_logical_key(
    thread_id: int,
    body: str,
    actor: str | None,
    logical_send_key: str,
) -> dict:
    saved_request_id = None
    if current_app.config.get('SAAS_MODE') and logical_send_key.startswith('reply-request:'):
        saved_request_id = int(logical_send_key.removeprefix('reply-request:'))
        saved = OutboundRequest.query.filter_by(id=saved_request_id, kind='reply', thread_id=thread_id).populate_existing().first()
        if saved is None:
            raise ValueError('Saved reply request does not belong to this conversation.')
        if saved.status in {'accepted', 'failed', 'ambiguous'} and saved.result_json:
            return json.loads(saved.result_json)
    thread = db.session.get(InboxThread, thread_id)
    if thread is None:
        return {'success': False, 'error': 'thread_not_found'}

    reply_body = normalize_sms_body((body or '').strip())
    if not reply_body:
        return {'success': False, 'error': 'empty_message'}

    result = None
    if saved_request_id:
        saved_payload = json.loads(saved.payload_json)
        if saved_payload['message_body'] != reply_body or saved_payload['intent']['phone'] != thread.phone:
            raise ValueError('The conversation or reply content changed from the saved request.')
        attempt = MessageDispatchAttempt.query.filter_by(logical_send_key=logical_send_key).populate_existing().first()
        if attempt is not None:
            if attempt.recipient_phone != thread.phone or attempt.message_body_digest != hashlib.sha256(reply_body.encode()).hexdigest():
                raise ValueError('Saved reply attempt does not match this recipient and content.')
            if attempt.status in {'sent', 'failed', 'sending', 'ambiguous'}:
                result = {
                    'success': attempt.status == 'sent', 'sid': attempt.provider_message_sid,
                    'status': attempt.status, 'error': attempt.last_error,
                    'logical_send_key': logical_send_key, 'idempotent_replay': True,
                }
    if result is None and _unsubscribed_entry_for_phone(thread.phone):
        return {
            'success': False,
            'status': 'blocked_opt_out',
            'sid': None,
            'error': 'Cannot send to unsubscribed recipient. They must reply START to resubscribe.',
        }

    if result is None:
        try:
            twilio = get_twilio_service(thread.organization_id)
        except Exception as exc:
            # Constructing the configured client does not submit a message.
            result = {'success': False, 'status': 'blocked' if saved_request_id else 'failed', 'sid': None, 'error': str(exc)}
    if result is None:
        try:
            if current_app.config.get('SAAS_MODE'):
                result = twilio.send_message_with_attempt(
                    thread.phone, reply_body, logical_send_key, None, None, 'manual_reply',
                )
            else:
                result = twilio.send_message(thread.phone, reply_body, send_kind='manual_reply')
        except Exception as exc:
            if saved_request_id:
                # Recover from the persisted attempt after DB/process errors.
                # Its sent/ambiguous state, rather than this exception alone,
                # determines the outcome on the next exact-key execution.
                raise
            result = {'success': False, 'status': 'failed', 'sid': None, 'error': str(exc)}

    saved = None
    previous_result = {}
    if saved_request_id:
        # The attempt commits before its provider call. Lock the intent after
        # that boundary so concurrent recovery/replay records one inbox bubble
        # and completes the intent in the same database transaction.
        saved = OutboundRequest.query.filter_by(
            id=saved_request_id, kind='reply', thread_id=thread_id,
        ).populate_existing().with_for_update().one()
        previous_result = json.loads(saved.result_json) if saved.result_json else {}
        if saved.status in {'accepted', 'failed'} or (
            saved.status == 'ambiguous' and result.get('status') in {'sending', 'ambiguous'}
        ):
            db.session.commit()
            return previous_result

    if not result.get('success'):
        error_text = result.get('error') or ''
        if classify_failure(error_text) == 'opt_out':
            _upsert_unsubscribed(
                thread.phone,
                error_text or 'Manual reply blocked by Twilio opt-out',
                source='message_failure',
                name=_resolve_unsubscribed_name(thread.phone, thread=thread),
            )

    message = None
    if saved and previous_result.get('inbox_message_id'):
        message = InboxMessage.query.filter_by(id=previous_result['inbox_message_id'], thread_id=thread.id).first()
    if message is None:
        message = _append_inbox_message(
            thread, thread.phone, 'outbound', reply_body,
            message_sid=result.get('sid'), automation_source='manual',
            raw_payload={'actor': actor, 'outbound_request_id': saved_request_id} if saved else ({'actor': actor} if actor else None),
            delivery_status=result.get('status'), delivery_error=result.get('error'),
        )
    else:
        message.message_sid = result.get('sid')
        message.delivery_status = result.get('status')
        message.delivery_error = result.get('error')
    if saved:
        from app.services.outbound_request_service import complete_request
        db.session.flush()
        result = dict(result, inbox_message_id=message.id)
        complete_request(saved, result)
    db.session.commit()
    record_usage_candidates(
        thread.organization_id,
        [result],
        source='reply',
    )
    return result


def send_thread_reply(thread_id: int, body: str, actor: str | None = None) -> dict:
    return _send_thread_reply_with_logical_key(
        thread_id,
        body,
        actor,
        f"manual-direct:{thread_id}:{uuid4().hex}",
    )


def send_thread_reply_with_key(
    thread_id: int,
    body: str,
    actor: str | None,
    logical_send_key: str,
) -> dict:
    return _send_thread_reply_with_logical_key(
        thread_id,
        body,
        actor,
        logical_send_key,
    )


def _normalize_unsubscribed_name(name: str | None) -> str | None:
    normalized = (name or '').strip()
    if not normalized:
        return None
    return normalized[:100]


def _community_member_for_phone(phone: str) -> CommunityMember | None:
    member = CommunityMember.query.filter_by(phone=phone).first()
    if member is not None:
        return member

    variants = phone_lookup_variants(phone)
    if not variants:
        return None

    return (
        CommunityMember.query
        .filter(phone_digits_sql(CommunityMember.phone).in_(variants))
        .order_by(CommunityMember.id.desc())
        .first()
    )


def _thread_for_phone(phone: str) -> InboxThread | None:
    thread = InboxThread.query.filter_by(phone=phone).first()
    if thread is not None:
        return thread

    variants = phone_lookup_variants(phone)
    if not variants:
        return None

    return (
        InboxThread.query
        .filter(phone_digits_sql(InboxThread.phone).in_(variants))
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

    variants = phone_lookup_variants(phone)
    if not variants:
        return []

    return (
        EventRegistration.query
        .filter(phone_digits_sql(EventRegistration.phone).in_(variants))
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

    variants = phone_lookup_variants(normalized_phone)
    if not variants:
        return None

    return (
        UnsubscribedContact.query
        .filter(phone_digits_sql(UnsubscribedContact.phone).in_(variants))
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
    # These columns deliberately store UTC without a timezone. Passing aware
    # values lets PostgreSQL cast through its session timezone and shifts them.
    now = utc_now().replace(tzinfo=None)
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


def _send_automated_reply_with_key(
    phone: str,
    thread: InboxThread,
    body: str,
    *,
    source: str,
    source_id: int | None,
    logical_send_key: str,
) -> dict:
    body = normalize_sms_body((body or '').strip())
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
        twilio = get_twilio_service(thread.organization_id)
        if current_app.config.get('SAAS_MODE'):
            result = twilio.send_message_with_attempt(
                phone,
                body,
                logical_send_key,
                None,
                None,
                'automation_reply',
            )
        else:
            result = twilio.send_message(
                phone,
                body,
                send_kind='automation_reply',
            )
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
    record_usage_candidates(
        thread.organization_id,
        [result],
        source=source,
    )
    return result


def _send_automated_reply(
    phone: str,
    thread: InboxThread,
    body: str,
    *,
    source: str,
    source_id: int | None,
) -> dict:
    return _send_automated_reply_with_key(
        phone,
        thread,
        body,
        source=source,
        source_id=source_id,
        logical_send_key=f"automation-direct:{thread.id}:{uuid4().hex}",
    )


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
    # Feedback completion is never a registration transition. Both the frozen
    # session purpose and the current flow are checked so a later flow edit
    # cannot turn saved feedback into an attendee name or signup.
    if session.purpose == 'event_feedback' or (survey and survey.is_event_feedback):
        return
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
        purpose=survey.purpose or 'survey',
        status='active',
        current_question_index=0,
        started_at=now,
        last_activity_at=now,
    )
    db.session.add(session)
    if survey.is_event_feedback:
        with db.session.no_autoflush:
            session.feedback_event_id = survey.feedback_event_id
            session.feedback_event_title = survey.feedback_event.title if survey.feedback_event else None
            session.feedback_questions_json = json.dumps(survey.questions)
            session.feedback_completion_message = survey.completion_message
    survey.start_count = (survey.start_count or 0) + 1

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


def _questions_for_session(session: SurveySession) -> list[str]:
    if session.purpose == 'event_feedback':
        try:
            questions = json.loads(session.feedback_questions_json or '[]')
        except (ValueError, TypeError):
            return []
        if not isinstance(questions, list) or not all(isinstance(question, str) for question in questions):
            return []
        return questions
    return session.survey.questions


def _advance_survey(session: SurveySession, inbound_text: str) -> list[str]:
    now = utc_now()
    survey = session.survey
    questions = _questions_for_session(session)
    if session.purpose == 'event_feedback' and not questions:
        session.status, session.completed_at, session.last_activity_at = 'cancelled', now, now
        return ['This feedback form is no longer available. Please contact the organizer.']
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
        completion_message = (session.feedback_completion_message
                              if session.purpose == 'event_feedback' else survey.completion_message)
        if completion_message:
            replies.append(completion_message.strip())
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
    questions = _questions_for_session(session)
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
    messaging_profile = resolve_messaging_profile(payload) if saas_mode_enabled() else None

    if not raw_from:
        return {'status': 'ignored', 'reason': 'missing_from'}
    if saas_mode_enabled() and messaging_profile is None:
        return {'status': 'ignored', 'reason': 'missing_destination_profile'}
    phone = normalize_phone(raw_from)
    if not validate_phone(phone):
        return {'status': 'ignored', 'reason': 'invalid_phone', 'phone': phone}

    organization_scope = (
        organization_context(messaging_profile.organization_id)
        if saas_mode_enabled() and messaging_profile is not None
        else nullcontext()
    )
    with organization_scope:
        def duplicate_result(existing_message):
            result = {'status': 'duplicate', 'thread_id': existing_message.thread_id}
            if saas_mode_enabled() and messaging_profile is not None:
                from app.services.automation_outbox_service import enqueue_for_inbound
                result['automation_outbox'] = enqueue_for_inbound(messaging_profile.organization_id, existing_message.id)
            return result

        if message_sid:
            if saas_mode_enabled() and messaging_profile is not None:
                from app.services.automation_outbox_service import resume_saved_inbound_sid
                saved_duplicate = resume_saved_inbound_sid(messaging_profile.organization_id, message_sid)
                if saved_duplicate is not None:
                    return saved_duplicate
            existing = InboxMessage.query.filter_by(message_sid=message_sid).first()
            if existing:
                return duplicate_result(existing)

        thread = _get_or_create_thread(phone, profile_name)
        try:
            if saas_mode_enabled():
                db.session.flush()
                thread = InboxThread.query.filter_by(id=thread.id).populate_existing().with_for_update().one()
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
            return duplicate_result(exc.existing_message)
        except IntegrityError:
            db.session.rollback()
            if message_sid:
                existing = InboxMessage.query.filter_by(message_sid=message_sid).first()
                if existing:
                    return duplicate_result(existing)
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

        # Destination recognition and authenticated ingestion do not depend on
        # outbound readiness. In-flight callbacks must retain recipient consent
        # even while billing, sender setup or the provider is paused.
        outbound_ready = True
        if messaging_profile is not None:
            from app.services.billing_service import organization_can_transmit_messages

            outbound_ready = organization_can_transmit_messages(messaging_profile.organization)

        def record_transport_consent(*, source: str, decision: str) -> None:
            if messaging_profile is None:
                return
            from app.services.recipient_permission_service import record_permission_evidence

            record_permission_evidence(
                messaging_profile.organization_id,
                phone,
                purpose='all',
                decision=decision,
                source=source,
                evidence_key=message_sid or f'inbound:{inbound_message.id}',
                occurred_at=inbound_message.created_at,
                metadata={'keyword': normalized},
            )

        session = _active_session(phone)
        pending_survey_reply = False
        if session is not None and saas_mode_enabled():
            from app.services.automation_outbox_service import session_has_unsubmitted_reply
            pending_survey_reply = session_has_unsubmitted_reply(session.id)
        # A complete feedback keyword is an explicit command, including when
        # an older signup is awaiting a name. Never turn it into that answer.
        # Keep paused forms recognizable so they cannot consume saved work.
        feedback_command = None
        if outbound_ready and normalized not in STOP_KEYWORDS:
            active_survey = SurveyFlow.query.filter_by(
                trigger_keyword=normalized, is_active=True,
            ).first()
            active_rule = None
            if active_survey is None:
                active_rule = KeywordAutomationRule.query.filter_by(
                    keyword=normalized, is_active=True,
                ).first()
            # Preserved/imported rows can predate namespace validation. Prefer
            # an active owner over an older paused row; current editors reject
            # reuse rather than releasing a paused form's trigger.
            if active_survey is not None and active_survey.is_event_feedback:
                feedback_command = active_survey
            elif (active_rule is None and session is not None
                    and session.purpose == 'event_feedback'
                    and normalize_keyword(session.survey.trigger_keyword) == normalized
                    and (active_survey is None or active_survey.id == session.survey_id)):
                feedback_command = session.survey
            elif active_survey is None and active_rule is None:
                feedback_command = SurveyFlow.query.filter_by(
                    trigger_keyword=normalized, purpose='event_feedback',
                ).order_by(SurveyFlow.id.desc()).first()
        if session is not None and outbound_ready and normalized not in STOP_KEYWORDS:
            inbound_message.automation_source = 'survey'
            inbound_message.automation_source_id = session.survey_id
        if normalized in STOP_KEYWORDS:
            _upsert_unsubscribed(
                phone,
                'Inbound STOP keyword received',
                source='inbound',
                name=_resolve_unsubscribed_name(phone, thread=thread),
            )
            record_transport_consent(source='inbound_stop', decision='denied')
            _cancel_active_sessions(phone)
            status = 'opt_out'
        elif not outbound_ready:
            if normalized in START_KEYWORDS and (
                normalized != 'YES'
                or (session is None and _unsubscribed_entry_for_phone(phone) is not None)
            ):
                _remove_unsubscribed(phone)
                # START removes the transport opt-out; it is not evidence of
                # permission for every broadcast purpose or an imported list.
                record_transport_consent(source='inbound_start', decision='unknown')
                status = 'opt_in'
            # Keep ordinary inbound messages without advancing a survey or
            # matching a keyword that cannot currently deliver its next reply.
        elif feedback_command is not None:
            inbound_message.automation_source = 'survey'
            inbound_message.automation_source_id = feedback_command.id
            matched_keyword = feedback_command.trigger_keyword
            if (session is not None and session.survey_id == feedback_command.id
                    and session.purpose == 'event_feedback'):
                # Repeating the keyword is not an answer or a fresh start.
                # An in-flight prompt already has its own durable identity.
                if pending_survey_reply:
                    status = 'survey_reply_pending'
                else:
                    questions = _questions_for_session(session)
                    question_index = session.current_question_index or 0
                    if 0 <= question_index < len(questions):
                        pending_replies.append({
                            'source': 'survey', 'source_id': session.survey_id,
                            'body': questions[question_index],
                        })
                        status = 'survey_prompt_repeated'
                    else:
                        now = utc_now()
                        session.status = 'cancelled'
                        session.completed_at = session.last_activity_at = now
                        pending_replies.append({
                            'source': 'system', 'source_id': None,
                            'body': 'This feedback form is no longer available. Please contact the organizer.',
                        })
                        status = 'feedback_unavailable'
            elif not feedback_command.is_active:
                pending_replies.append({
                    'source': 'system', 'source_id': None,
                    'body': 'This feedback form is paused. Your existing conversation has been kept. Please contact the organizer.',
                })
                status = 'feedback_paused'
            else:
                # The existing start helper cancels, but never completes or
                # deletes, old sessions. Their unsent questions remain bound
                # to the cancelled session; the outbox gate cancels them while
                # preserving any previously accepted or uncertain submission.
                session, replies = _start_survey(feedback_command, thread, phone)
                for reply in replies:
                    pending_replies.append({
                        'source': 'survey', 'source_id': feedback_command.id,
                        'body': reply,
                    })
                status = 'survey_started'
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
        elif session and pending_survey_reply:
            # Keep the text in the inbox; no answer can advance a question that
            # is still waiting for its original provider submission.
            status = 'survey_reply_pending'
        else:
            if session:
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
                record_transport_consent(source='inbound_start', decision='unknown')
                start_reply = (
                    'Your SMS opt-out has been removed.'
                    if was_unsubscribed
                    else 'No SMS opt-out is recorded for this number.'
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
                    if survey and survey.is_event_feedback and candidate != normalized:
                        survey = None
                        continue
                    if survey:
                        matched_keyword = candidate
                        break

                if survey:
                    inbound_message.automation_source = 'survey'
                    inbound_message.automation_source_id = survey.id
                    _session, replies = _start_survey(survey, thread, phone)
                    session = _session
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
                        inbound_message.automation_source = 'keyword'
                        inbound_message.automation_source_id = rule.id
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
                        record_transport_consent(source='inbound_start', decision='unknown')
                        start_reply = (
                            'Your SMS opt-out has been removed.'
                            if was_unsubscribed
                            else 'No SMS opt-out is recorded for this number.'
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

        if saas_mode_enabled():
            from app.services.automation_outbox_service import enqueue_for_inbound, save_automation_replies

            saved_replies = save_automation_replies(
                inbound_message, thread, pending_replies, survey_session=session,
                question_delay=_survey_start_question_delay_seconds() if status == 'survey_started' else 0,
            )
            inbound_id, thread_id = inbound_message.id, thread.id
            db.session.commit()
            queued = enqueue_for_inbound(messaging_profile.organization_id, inbound_id)
            return {
                'status': status, 'thread_id': thread_id, 'phone': phone,
                'replies': [{'source': row.source, 'result': {'status': row.status, 'success': None, 'automation_outbox_id': row.id}} for row in saved_replies],
                'organization_id': messaging_profile.organization_id, 'automation_blocked': not outbound_ready,
                'automation_outbox': queued,
            }

        db.session.commit()

        survey_reply_count = 0
        for pending_index, pending in enumerate(pending_replies):
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
                    'result': _send_automated_reply_with_key(
                        phone,
                        thread,
                        body,
                        source=source,
                        source_id=source_id if isinstance(source_id, int) else None,
                        logical_send_key=(
                            f"automation:{inbound_message.id}:{pending_index}:"
                            f"{source}:{source_id if isinstance(source_id, int) else 0}"
                        ),
                    ),
                }
            )

        db.session.commit()
        return {
            'status': status,
            'thread_id': thread.id,
            'phone': phone,
            'replies': sent_replies,
            'organization_id': messaging_profile.organization_id if messaging_profile is not None else None,
            'automation_blocked': not outbound_ready,
        }
