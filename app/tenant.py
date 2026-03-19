from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from flask import current_app, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria


_current_organization_id: ContextVar[int | None] = ContextVar("current_organization_id", default=None)
_tenant_scope_disabled: ContextVar[bool] = ContextVar("tenant_scope_disabled", default=False)
_tenant_events_initialized = False


def saas_mode_enabled() -> bool:
    return has_app_context() and bool(current_app.config.get("SAAS_MODE"))


def get_current_organization_id() -> int | None:
    return _current_organization_id.get()


def set_current_organization_id(organization_id: int | None):
    return _current_organization_id.set(int(organization_id)) if organization_id else _current_organization_id.set(None)


def clear_current_organization_id() -> None:
    _current_organization_id.set(None)


@contextmanager
def organization_context(organization_id: int | None):
    token = set_current_organization_id(organization_id)
    try:
        yield
    finally:
        _current_organization_id.reset(token)


@contextmanager
def without_tenant_scope():
    token = _tenant_scope_disabled.set(True)
    try:
        yield
    finally:
        _tenant_scope_disabled.reset(token)


def tenant_scope_is_active() -> bool:
    return saas_mode_enabled() and not _tenant_scope_disabled.get() and get_current_organization_id() is not None


def _tenant_scoped_models():
    from app.models import (
        AuthEvent,
        CommunityMember,
        Event,
        EventRegistration,
        InboxMessage,
        InboxThread,
        KeywordAutomationRule,
        MessageLog,
        OrganizationInvitation,
        SuppressedContact,
        OrganizationSubscription,
        ScheduledMessage,
        SurveyFlow,
        SurveyResponse,
        SurveySession,
        UnsubscribedContact,
    )

    return (
        AuthEvent,
        CommunityMember,
        Event,
        EventRegistration,
        InboxMessage,
        InboxThread,
        KeywordAutomationRule,
        MessageLog,
        OrganizationInvitation,
        SuppressedContact,
        OrganizationSubscription,
        ScheduledMessage,
        SurveyFlow,
        SurveyResponse,
        SurveySession,
        UnsubscribedContact,
    )


def init_tenant_scoping() -> None:
    global _tenant_events_initialized
    if _tenant_events_initialized:
        return
    _tenant_events_initialized = True

    @event.listens_for(Session, "do_orm_execute")
    def _add_tenant_criteria(execute_state):
        if not execute_state.is_select or not tenant_scope_is_active():
            return

        organization_id = get_current_organization_id()
        statement = execute_state.statement
        for model in _tenant_scoped_models():
            statement = statement.options(
                with_loader_criteria(
                    model,
                    lambda cls: cls.organization_id == organization_id,
                    include_aliases=True,
                )
            )
        execute_state.statement = statement

    @event.listens_for(Session, "before_flush")
    def _assign_organization_id(session, flush_context, instances):
        if not tenant_scope_is_active():
            return

        organization_id = get_current_organization_id()
        tenant_model_classes = _tenant_scoped_models()
        for obj in session.new:
            if not isinstance(obj, tenant_model_classes):
                continue
            if getattr(obj, "organization_id", None) is None:
                setattr(obj, "organization_id", organization_id)
